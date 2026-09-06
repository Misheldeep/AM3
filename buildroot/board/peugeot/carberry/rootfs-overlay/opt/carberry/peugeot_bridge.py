#!/usr/bin/env python3
"""
Peugeot / CarBerry bridge dev04

Target hardware:
  - Raspberry Pi 1 Model B Rev 2 (revision 000e)
  - CarBerry HW 1.00 / PIC FW 1.19
  - direct UART ownership: /dev/ttyAMA0, 115200 8N1
  - CH1 Peugeot 125 kbit/s
  - CH2 miniDSP Harmony 250 kbit/s

Key dev04 changes versus dev03:
  - carberry_d is no longer in the production data path;
  - CAN USER TX is pipelined over direct UART, never wait-for-OK per frame;
  - software TX whitelist at the lowest CAN-send layer;
  - hardware RX acceptance filters in production, catch-all debug mode on demand;
  - Peugeot steering-wheel decoder (0x21F);
  - Peugeot display-button decoder/emulator (0x3E5);
  - Harmony state decoder and Master/Sub volume controller;
  - fixed Raspberry Pi GPIO allocation for local display joystick and BT controls;
  - GPIO via /dev/gpiochip0 only; joystick lines requested/read as one group;
  - local Unix API retained for sniffer/debug/emulators.

Important protocol status:
  - all commands marked CONFIRMED below were observed on the real Harmony bus;
  - the Master/Sub page-toggle *sequence* is reconstructed from observed traffic
    and must still be verified end-to-end when the car is next available.
"""

import heapq
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
from collections import deque
from pathlib import Path


DEV_VERSION = "dev04-direct-uart-alpha6-autoboot"
UART_DEVICE = "/dev/ttyAMA0"
UART_BAUD = termios.B115200
API_SOCKET = Path("/run/peugeot-bridge.sock")

# Permanent project wiring. Never swap these channels.
CHANNELS = {
    1: ("PEUGEOT", "125K"),
    2: ("HARMONY", "250K"),
}

# Production hardware RX whitelist.
# CH1 is intentionally strict: vehicle bus traffic is heavy and almost all of
# it is irrelevant to this bridge.
PRODUCTION_RX_FILTERS = {
    1: (0x021F, 0x03E5),
    2: (0x0201, 0x0202),
}

# Lowest-level software TX whitelist. There is intentionally no normal API
# path around this check.
TX_ALLOW = {
    1: {
        0x03E5,  # Peugeot MFD / head-unit buttons
        0x0165,  # HU heartbeat
        0x00A4,  # track metadata
        0x0325,  # playback state
        0x03A5,  # track position
    },
    2: {
        0x0202,  # Harmony remote -> DSP commands
    },
}

# Direct-UART pipeline. 500 consecutive TX commands were verified 500/500,
# in order, with no errors; a small outstanding window retains low latency for
# newly-arriving high-priority controls while keeping the UART continuously fed.
MAX_OUTSTANDING = 8
MAX_UART_TXBUF = 65536
COMMAND_TIMEOUT = 1.0

# First field run safety: console/API emulators remain available even if this
# is False. Flip to True after the page-toggle sequence has been verified on
# the real Harmony bus.
AUTOCONTROL_DEFAULT = os.environ.get(
    "PEUGEOT_AUTOCONTROL_DEFAULT", "0"
).strip().lower() in ("1", "on", "true", "yes", "enabled")

# Raspberry Pi 1 Model B Rev2 / 26-pin P1 allocation, no GPIO expander.
GPIO_JOYSTICK = {
    "UP": 10,      # P1-19
    "DOWN": 9,    # P1-21
    "LEFT": 11,   # P1-23
    "RIGHT": 8,   # P1-24
    "OK": 7,      # P1-26, joystick MID
    "MENU": 25,   # P1-22, joystick SET
    "DARK": 4,    # P1-7, joystick RST
}
GPIO_BT = {
    "PLAY_PAUSE": 22,  # P1-15
    "NEXT": 23,        # P1-16
    "PREVIOUS": 24,    # P1-18
}

# Bluetooth electrical interface is intentionally disabled until the QCC5181
# button input voltage/driver wiring is finalized. Pin allocation is fixed.
# Supported later: "active_high" (external transistor driver) or
# "open_drain_low" (only if electrically proven safe for direct connection).
BT_GPIO_MODE = os.environ.get("PEUGEOT_BT_GPIO_MODE", "disabled").strip().lower()
BT_PULSE_SECONDS = 0.080

GPIO_DEBOUNCE = 0.025
GPIO_POLL_PERIOD = 0.005
JOYSTICK_REPEAT_DELAY = 0.400
JOYSTICK_REPEAT_SLOW = 0.200
JOYSTICK_REPEAT_FAST_AFTER = 1.500
JOYSTICK_REPEAT_FAST = 0.100

DISPLAY_ONE_SHOT_RELEASE = 0.080

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


class BridgeLog:
    def write(self, direction, text, force_sync=False):
        mono = time.monotonic()
        print(f"{mono:14.6f}  {direction:<7}  {text}", flush=True)

    def close(self):
        pass


# CarBerry USER receive format with RIGHT alignment.
CAN_RX_RE = re.compile(
    r"^RX(?P<channel>[12])\s+"
    r"(?:(?P<hwts>[0-9A-Fa-f]{10})\s+)?"
    r"(?P<canid>[0-9A-Fa-f]{4}(?::[0-9A-Fa-f]{6})?)-"
    r"(?P<data>[0-9A-Fa-f]*)$"
)


def parse_can_rx(line):
    match = CAN_RX_RE.match(line)
    if not match:
        return None
    return {
        "channel": int(match.group("channel")),
        "hardware_timestamp": match.group("hwts"),
        "id": match.group("canid").upper(),
        "data": match.group("data").upper(),
        "raw": line,
    }


def parse_hex_bytes(text):
    text = str(text).replace(" ", "").strip()
    if len(text) % 2:
        raise ValueError("hex payload has odd length")
    return bytes.fromhex(text)


class PendingCommand:
    __slots__ = (
        "command", "origin", "kind", "queued_mono", "sent_mono",
        "payload_lines", "callback",
    )

    def __init__(self, command, origin, kind="control", callback=None):
        self.command = command
        self.origin = origin
        self.kind = kind
        self.queued_mono = time.monotonic()
        self.sent_mono = None
        self.payload_lines = []
        self.callback = callback


class CarBerryUART:
    """Direct owner of the Pi<->CarBerry PIC UART."""

    def __init__(self, logger, can_rx_callback=None):
        self.log = logger
        self.can_rx_callback = can_rx_callback
        self.fd = None
        self.rxbuf = bytearray()
        self.txbuf = bytearray()
        self.pending = deque()
        self.open()

    def open(self):
        self.fd = os.open(
            UART_DEVICE,
            os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
        )

        a = termios.tcgetattr(self.fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = UART_BAUD
        a[5] = UART_BAUD
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

        self.log.write(
            "INFO",
            f"Direct UART opened: {UART_DEVICE} 115200 8N1",
            force_sync=True,
        )

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    def set_can_rx_callback(self, callback):
        self.can_rx_callback = callback

    def _write_blocking_setup(self, data, timeout=1.0):
        pos = 0
        deadline = time.monotonic() + timeout
        while pos < len(data):
            if time.monotonic() >= deadline:
                raise TimeoutError("UART setup write timeout")
            try:
                n = os.write(self.fd, data[pos:])
                if n:
                    pos += n
                    continue
            except BlockingIOError:
                pass
            select.select([], [self.fd], [], 0.010)

    def _read_available(self):
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            if not data:
                break
            self.rxbuf.extend(data)

    def _extract_lines(self):
        lines = []
        while True:
            pos = self.rxbuf.find(b"\r\n")
            if pos < 0:
                break
            raw = bytes(self.rxbuf[:pos])
            del self.rxbuf[:pos + 2]
            lines.append(raw.decode("ascii", errors="backslashreplace"))
        return lines

    def _route_can(self, line, mono=None):
        frame = parse_can_rx(line)
        if frame is None:
            return False
        if mono is None:
            mono = time.monotonic()
        if self.can_rx_callback is not None:
            self.can_rx_callback(mono, frame)
        return True

    def command_sync(self, command, timeout=1.5, quiet=False):
        """Setup/shutdown only. Runtime TX uses queue_command()."""
        if self.pending or self.txbuf:
            raise RuntimeError("command_sync called with asynchronous traffic pending")

        if not quiet:
            self.log.write("CMD", command)
        self._write_blocking_setup((command + "\r").encode("ascii"), timeout)

        payload = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not stop_requested:
            self._read_available()
            for line in self._extract_lines():
                if self._route_can(line):
                    continue
                if not quiet or line.startswith("ERROR"):
                    self.log.write("RX", line)
                if line == "OK":
                    return True, payload
                if line.startswith("ERROR"):
                    return False, payload
                payload.append(line)

            left = deadline - time.monotonic()
            if left > 0:
                select.select([self.fd], [], [], min(0.020, left))

        self.log.write("ERR", f"COMMAND TIMEOUT: {command}", force_sync=True)
        return False, payload

    def queue_command(self, command, origin, kind="control", callback=None):
        data = (str(command) + "\r").encode("ascii")
        if len(self.txbuf) + len(data) > MAX_UART_TXBUF:
            self.log.write(
                "ERR",
                f"UART TX queue overflow; rejected origin={origin} command={command}",
                force_sync=True,
            )
            if callback is not None:
                callback(False, [], "queue_overflow")
            return False

        item = PendingCommand(command, origin, kind, callback)
        self.pending.append(item)
        self.txbuf.extend(data)
        return True

    def queue_can(self, channel, can_id, payload, origin, callback=None):
        channel = int(channel)
        can_id = int(can_id)
        payload = bytes(payload)

        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        if not 0 <= can_id <= 0x7FF:
            raise ValueError("only standard 11-bit CAN IDs are allowed")
        if len(payload) > 8:
            raise ValueError("CAN payload exceeds 8 bytes")

        if can_id not in TX_ALLOW[channel]:
            self.log.write(
                "BLOCK",
                f"TX whitelist rejected CH{channel} ID={can_id:03X} "
                f"data={payload.hex().upper()} origin={origin}",
                force_sync=True,
            )
            if callback is not None:
                callback(False, [], "tx_whitelist")
            return False

        command = (
            f"CAN USER TX CH{channel} {can_id:04X} "
            f"{payload.hex().upper()}"
        )
        return self.queue_command(command, origin, kind="can_tx", callback=callback)

    def _finish_pending(self, ok, terminal_line):
        if not self.pending:
            self.log.write("WARN", f"stray terminal response: {terminal_line}")
            return

        item = self.pending.popleft()
        if item.callback is not None:
            try:
                item.callback(ok, list(item.payload_lines), terminal_line)
            except Exception as exc:
                self.log.write(
                    "ERR",
                    f"command callback failed origin={item.origin}: {exc!r}",
                    force_sync=True,
                )

        if not ok:
            self.log.write(
                "ERR",
                f"command failed origin={item.origin} command={item.command} "
                f"response={terminal_line}",
                force_sync=True,
            )

    def _handle_line(self, line):
        mono = time.monotonic()
        if self._route_can(line, mono):
            return

        if line == "OK":
            self._finish_pending(True, line)
            return

        if line.startswith("ERROR"):
            self._finish_pending(False, line)
            return

        if self.pending:
            self.pending[0].payload_lines.append(line)
        else:
            self.log.write("RX", line)

    def pump(self):
        """Nonblocking read/write service. Never waits for command completion."""
        if self.fd is None:
            return

        # Read first so heavy vehicle traffic cannot starve RX while TX is busy.
        self._read_available()
        for line in self._extract_lines():
            self._handle_line(line)

        # The pending deque includes commands already queued into txbuf. Limit
        # *logical* command admission in the scheduler, not UART byte writes.
        if self.txbuf:
            try:
                n = os.write(self.fd, self.txbuf)
                if n > 0:
                    del self.txbuf[:n]
            except BlockingIOError:
                pass

        self._read_available()
        for line in self._extract_lines():
            self._handle_line(line)

        self._check_timeouts()

    def _check_timeouts(self):
        if not self.pending:
            return
        oldest = self.pending[0]
        age = time.monotonic() - oldest.queued_mono
        if age > COMMAND_TIMEOUT:
            # Do not silently re-associate subsequent OK responses after one
            # timeout. The UART protocol has lost framing at that point.
            raise TimeoutError(
                f"CarBerry command response timeout age={age:.3f}s "
                f"origin={oldest.origin} command={oldest.command}"
            )

    def can_accept_more(self):
        return len(self.pending) < MAX_OUTSTANDING and len(self.txbuf) < 8192

    def snapshot(self):
        return {
            "device": UART_DEVICE,
            "pending_commands": len(self.pending),
            "tx_buffer_bytes": len(self.txbuf),
            "max_outstanding": MAX_OUTSTANDING,
        }


class EventHub:
    def __init__(self, logger):
        self.log = logger
        self.api = None
        self.router = None

    def attach_api(self, api):
        self.api = api

    def attach_router(self, router):
        self.router = router

    def can_rx(self, mono, frame):
        event = {
            "event": "can",
            "mono": mono,
            "direction": "rx",
            "channel": frame["channel"],
            "id": frame["id"],
            "data": frame["data"],
            "hardware_timestamp": frame["hardware_timestamp"],
            "raw": frame["raw"],
        }
        if self.api is not None:
            self.api.publish(event)
        if self.router is not None:
            self.router.handle_can(mono, frame)

    def can_tx(self, mono, channel, can_id, payload, origin):
        event = {
            "event": "can",
            "mono": mono,
            "direction": "tx",
            "channel": channel,
            "id": f"{can_id:04X}",
            "data": bytes(payload).hex().upper(),
            "origin": origin,
        }
        if self.api is not None:
            self.api.publish(event)

    def semantic(self, name, **fields):
        event = {
            "event": "semantic",
            "mono": time.monotonic(),
            "name": name,
        }
        event.update(fields)
        if self.api is not None:
            self.api.publish(event)


# ---------------------------------------------------------------------------
# CarBerry status LEDs
# ---------------------------------------------------------------------------


class StatusLEDs:
    GREEN = "LED1"
    RED = "LED2"

    PATTERN_INITIALIZING = ((True, 0.250), (False, 0.750))
    PATTERN_UNSYNCED = ((True, 0.150), (False, 1.850))
    PATTERN_SYNCED = (
        (True, 0.150), (False, 0.150),
        (True, 0.150), (False, 1.550),
    )
    PATTERN_ERROR = ((True, 0.125), (False, 0.125))

    def __init__(self, cb):
        self.cb = cb
        self.enabled = True
        self.green = None
        self.red = None
        self.mode = "unknown"
        self.pattern_led = None
        self.pattern = ()
        self.pattern_index = 0
        self.next_transition = 0.0

    def _set(self, led, on):
        if not self.enabled:
            return
        current = self.green if led == self.GREEN else self.red
        if current is on:
            return
        action = "SET" if on else "CLEAR"

        def done(ok, payload, terminal):
            if not ok:
                self.enabled = False

        if self.cb.queue_command(
            f"GPLED {led} {action}",
            f"STATUS LED {led} {action}",
            callback=done,
        ):
            if led == self.GREEN:
                self.green = on
            else:
                self.red = on

    def _stop_pattern(self):
        self.pattern_led = None
        self.pattern = ()
        self.pattern_index = 0
        self.next_transition = 0.0

    def _start_pattern(self, led, pattern):
        self.pattern_led = led
        self.pattern = pattern
        self.pattern_index = 0
        state, duration = pattern[0]
        self._set(led, state)
        self.next_transition = time.monotonic() + duration

    def initializing(self):
        self.mode = "initializing"
        self._stop_pattern()
        self._set(self.GREEN, False)
        self._start_pattern(self.RED, self.PATTERN_INITIALIZING)

    def can_ready(self):
        self.mode = "can_ready"
        self._stop_pattern()
        self._set(self.RED, False)
        self._set(self.GREEN, False)
        self._start_pattern(self.GREEN, self.PATTERN_UNSYNCED)

    def synced(self):
        if self.mode == "synced":
            return
        self.mode = "synced"
        self._stop_pattern()
        self._set(self.RED, False)
        self._set(self.GREEN, False)
        self._start_pattern(self.GREEN, self.PATTERN_SYNCED)

    def error(self):
        self.mode = "error"
        self._stop_pattern()
        self._set(self.GREEN, False)
        self._set(self.RED, False)
        self._start_pattern(self.RED, self.PATTERN_ERROR)

    def tick(self):
        if not self.enabled or self.pattern_led is None or not self.pattern:
            return
        now = time.monotonic()
        if now < self.next_transition:
            return
        self.pattern_index = (self.pattern_index + 1) % len(self.pattern)
        state, duration = self.pattern[self.pattern_index]
        self._set(self.pattern_led, state)
        self.next_transition = now + duration

    def next_deadline(self):
        if not self.enabled or self.pattern_led is None:
            return None
        return self.next_transition


# ---------------------------------------------------------------------------
# Peugeot display / HU encoding retained from dev03
# ---------------------------------------------------------------------------

DISPLAY_BUTTONS = {
    "MENU": bytes.fromhex("40 00 00 00 00 00"),
    "EXIT": bytes.fromhex("00 00 10 00 00 00"),
    "OK": bytes.fromhex("00 00 40 00 00 00"),
    "DARK": bytes.fromhex("00 00 04 00 00 00"),
    "LEFT": bytes.fromhex("00 00 00 00 00 01"),
    "RIGHT": bytes.fromhex("00 00 00 00 00 04"),
    "DOWN": bytes.fromhex("00 00 00 00 00 40"),
    "UP": bytes.fromhex("00 00 00 00 00 10"),
}
DISPLAY_BUTTONS_BY_DATA = {value: key for key, value in DISPLAY_BUTTONS.items()}
DISPLAY_ZERO = bytes(6)

RU_TRANSLIT = {
    "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Е":"E","Ё":"Yo",
    "Ж":"Zh","З":"Z","И":"I","Й":"Y","К":"K","Л":"L","М":"M",
    "Н":"N","О":"O","П":"P","Р":"R","С":"S","Т":"T","У":"U",
    "Ф":"F","Х":"Kh","Ц":"Ts","Ч":"Ch","Ш":"Sh","Щ":"Sch","Ъ":"",
    "Ы":"Y","Ь":"","Э":"E","Ю":"Yu","Я":"Ya",
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo",
    "ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m",
    "н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
    "ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"",
    "ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
}


def transliterate_ru(text):
    return "".join(RU_TRANSLIT.get(ch, ch) for ch in text)


def encode_display_text(text, mode="translit"):
    text = "" if text is None else str(text)
    if mode == "translit":
        return transliterate_ru(text).encode("ascii", errors="replace")
    if mode == "ascii":
        return text.encode("ascii", errors="replace")
    if mode == "cp1251":
        return text.encode("cp1251", errors="replace")
    if mode == "low8":
        return bytes(ord(ch) & 0xFF for ch in text)
    raise ValueError(f"unknown text encoding mode: {mode}")


def encode_track_metadata(track_index, artist, title, text_mode="translit"):
    artist_bytes = encode_display_text(artist, text_mode)[:20].ljust(20, b"\x00")
    title_bytes = encode_display_text(title, text_mode)[:20].ljust(20, b"\x00")
    text = artist_bytes + title_bytes
    frames = [bytes((0x10, 0x2C, 0x20, 0x00, 0x98, track_index & 0xFF)) + text[:2]]
    offset = 2
    for prefix in range(0x21, 0x26):
        frames.append(bytes((prefix,)) + text[offset:offset + 7])
        offset += 7
    frames.append(bytes((0x26,)) + text[offset:offset + 3])
    return frames


def encode_track_position(track_index, duration, position):
    if duration is None:
        total_min = 0xFF
        total_sec = 0xFF
    else:
        duration = max(0, int(duration))
        total_min = min(duration // 60, 0xFF)
        total_sec = duration % 60
    position = max(0, int(position))
    pos_min = min(position // 60, 0xFF)
    pos_sec = position % 60
    return bytes((track_index & 0xFF, total_min, total_sec, pos_min, pos_sec, 0x00))


class TxScheduler:
    DISPLAY_ZERO_PERIOD = 0.500
    HU_HEARTBEAT_PERIOD = 0.100
    PLAYBACK_PERIOD = 0.500
    POSITION_PERIOD = 1.000

    # Lower value = higher priority.
    PRIO_CONTROL = 0
    PRIO_HEARTBEAT = 10
    PRIO_DISPLAY = 15
    PRIO_PLAYBACK = 20
    PRIO_METADATA = 30

    def __init__(self, cb, events, logger):
        self.cb = cb
        self.events = events
        self.log = logger
        self.queue = []
        self.queue_seq = 0

        self.display_enabled = False
        self.next_display_zero = None
        self.hu_enabled = False
        self.hu_source = 0xC4
        self.next_hu_heartbeat = None

        self.playback = "stopped"
        self.next_playback = None
        self.track_index = 1
        self.artist = ""
        self.title = ""
        self.text_mode = "translit"
        self.duration = None
        self.position_base = 0.0
        self.position_base_mono = time.monotonic()
        self.allow_overflow = False
        self.next_position = None

    def _enqueue(self, channel, can_id, payload, origin, due=None, priority=None):
        if due is None:
            due = time.monotonic()
        if priority is None:
            priority = self.PRIO_METADATA
        payload = bytes(payload)
        if not 0 <= can_id <= 0x7FF:
            raise ValueError("only standard 11-bit CAN IDs are supported")
        if len(payload) > 8:
            raise ValueError("CAN payload exceeds 8 bytes")
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        self.queue_seq += 1
        heapq.heappush(
            self.queue,
            (float(due), int(priority), self.queue_seq, channel, can_id, payload, origin),
        )

    def send_control(self, channel, can_id, payload, origin, due=None):
        self._enqueue(
            channel, can_id, payload, origin,
            due=due,
            priority=self.PRIO_CONTROL,
        )

    def _dispatch_one(self, channel, can_id, payload, origin):
        mono = time.monotonic()
        ok = self.cb.queue_can(channel, can_id, payload, origin)
        if ok:
            self.events.can_tx(mono, channel, can_id, payload, origin)
        else:
            self.events.semantic(
                "can.tx_rejected",
                origin=origin,
                channel=channel,
                can_id=f"{can_id:04X}",
                data=payload.hex().upper(),
            )
        return ok

    def display_enable(self, enabled):
        self.display_enabled = bool(enabled)
        self.next_display_zero = time.monotonic() if self.display_enabled else None
        self.events.semantic("display.enabled", enabled=self.display_enabled)

    def display_button(self, button, origin="API", release_delay=DISPLAY_ONE_SHOT_RELEASE):
        button = str(button).upper()
        if button not in DISPLAY_BUTTONS:
            raise ValueError(f"unknown DISPLAY button: {button}")
        now = time.monotonic()
        self._enqueue(
            1, 0x3E5, DISPLAY_BUTTONS[button],
            f"{origin} DISPLAY {button}",
            now, self.PRIO_DISPLAY,
        )
        self._enqueue(
            1, 0x3E5, DISPLAY_ZERO,
            f"{origin} DISPLAY RELEASE",
            now + max(0.020, float(release_delay)),
            self.PRIO_DISPLAY,
        )
        self.events.semantic("display.button", button=button, origin=origin)

    def hu_enable(self, enabled):
        self.hu_enabled = bool(enabled)
        self.next_hu_heartbeat = time.monotonic() if self.hu_enabled else None
        self.events.semantic("hu.enabled", enabled=self.hu_enabled)

    def hu_source_raw(self, value):
        if isinstance(value, str):
            value = value.strip()
            if value.lower().startswith("0x"):
                value = int(value, 16)
            elif len(value) <= 2 and all(c in "0123456789abcdefABCDEF" for c in value):
                value = int(value, 16)
            else:
                value = int(value, 10)
        value = int(value)
        if not 0 <= value <= 0xFF:
            raise ValueError("HU source byte must be 0..255")
        old = self.hu_source
        self.hu_source = value
        if self.hu_enabled:
            self.next_hu_heartbeat = time.monotonic()
        self.events.semantic("hu.source_raw", old=f"{old:02X}", value=f"{value:02X}")

    def _current_position(self, now=None):
        if now is None:
            now = time.monotonic()
        position = self.position_base
        if self.playback == "playing":
            position += max(0.0, now - self.position_base_mono)
        position = int(position)
        if not self.allow_overflow and self.duration is not None and position > self.duration:
            position = int(self.duration)
        return max(0, position)

    def set_position(self, position, allow_overflow=None):
        position = max(0, int(position))
        if allow_overflow is not None:
            self.allow_overflow = bool(allow_overflow)
        self.position_base = float(position)
        self.position_base_mono = time.monotonic()
        self._enqueue(
            1, 0x3A5,
            encode_track_position(self.track_index, self.duration, self._current_position()),
            "TRACK POSITION SET",
            priority=self.PRIO_PLAYBACK,
        )
        if self.playback == "playing":
            self.next_position = time.monotonic() + self.POSITION_PERIOD
        self.events.semantic("track.position", position=position, allow_overflow=self.allow_overflow)

    def set_playback(self, state):
        state = str(state).lower()
        if state not in ("playing", "paused", "stopped"):
            raise ValueError("playback state must be playing, paused or stopped")
        now = time.monotonic()
        current = self._current_position(now)
        self.position_base = float(current)
        self.position_base_mono = now
        self.playback = state
        if state == "stopped":
            self.next_playback = None
            self.next_position = None
        else:
            self.next_playback = now
            self.next_position = now if state == "playing" else None
        self.events.semantic("playback.state", state=state)

    def set_track(self, artist, title, duration=None, position=0,
                  track_index=1, text_mode="translit", allow_overflow=False,
                  playback=None):
        self.artist = "" if artist is None else str(artist)
        self.title = "" if title is None else str(title)
        self.track_index = int(track_index) & 0xFF
        if self.track_index == 0:
            self.track_index = 1
        self.text_mode = str(text_mode)
        encode_display_text(self.artist, self.text_mode)
        encode_display_text(self.title, self.text_mode)
        self.duration = None if duration is None else max(0, int(duration))
        self.allow_overflow = bool(allow_overflow)
        self.position_base = float(max(0, int(position)))
        self.position_base_mono = time.monotonic()

        now = time.monotonic()
        spacing = 0.003
        step = 0
        self._enqueue(1, 0x0A4,
                      bytes((0x04, 0x00, 0x00, 0x00, self.track_index)),
                      "TRACK PREAMBLE", now + step * spacing, self.PRIO_METADATA)
        step += 1
        self._enqueue(1, 0x3A5,
                      bytes((self.track_index, 0xFF, 0xFF, 0x00, 0x80, 0x80)),
                      "TRACK NO-POSITION", now + step * spacing, self.PRIO_METADATA)
        step += 1
        self._enqueue(1, 0x325, bytes((0x00, 0x0B, 0x00)),
                      "TRACK PRE-PLAY", now + step * spacing, self.PRIO_METADATA)
        step += 1
        for frame in encode_track_metadata(
                self.track_index, self.artist, self.title, self.text_mode):
            self._enqueue(1, 0x0A4, frame, "TRACK METADATA",
                          now + step * spacing, self.PRIO_METADATA)
            step += 1

        self.events.semantic(
            "track.set", artist=self.artist, title=self.title,
            duration=self.duration, position=int(self.position_base),
            track_index=self.track_index, text_mode=self.text_mode,
            allow_overflow=self.allow_overflow,
        )

        sequence_end = now + step * spacing
        if playback is not None:
            self.set_playback(playback)
            if self.playback in ("playing", "paused"):
                self.next_playback = sequence_end
            if self.playback == "playing":
                self.next_position = sequence_end
        elif self.playback == "playing":
            self.next_position = sequence_end

    def _schedule_periodic(self, now):
        if self.display_enabled:
            if self.next_display_zero is None:
                self.next_display_zero = now
            if now >= self.next_display_zero:
                self._enqueue(1, 0x3E5, DISPLAY_ZERO, "DISPLAY ZERO",
                              now, self.PRIO_DISPLAY)
                while self.next_display_zero <= now:
                    self.next_display_zero += self.DISPLAY_ZERO_PERIOD

        if self.hu_enabled:
            if self.next_hu_heartbeat is None:
                self.next_hu_heartbeat = now
            if now >= self.next_hu_heartbeat:
                self._enqueue(1, 0x165,
                              bytes((0xC0, self.hu_source, 0x20, 0x00)),
                              "HU HEARTBEAT", now, self.PRIO_HEARTBEAT)
                while self.next_hu_heartbeat <= now:
                    self.next_hu_heartbeat += self.HU_HEARTBEAT_PERIOD

        if self.playback in ("playing", "paused"):
            if self.next_playback is None:
                self.next_playback = now
            if now >= self.next_playback:
                payload = bytes((0x00, 0x0B, 0x00)) if self.playback == "playing" else bytes((0x00, 0x02, 0x00))
                self._enqueue(1, 0x325, payload,
                              f"PLAYBACK {self.playback.upper()}",
                              now, self.PRIO_PLAYBACK)
                while self.next_playback <= now:
                    self.next_playback += self.PLAYBACK_PERIOD

        if self.playback == "playing":
            if self.next_position is None:
                self.next_position = now
            if now >= self.next_position:
                self._enqueue(1, 0x3A5,
                              encode_track_position(
                                  self.track_index, self.duration,
                                  self._current_position(now)),
                              "TRACK POSITION TICK", now, self.PRIO_PLAYBACK)
                while self.next_position <= now:
                    self.next_position += self.POSITION_PERIOD

    def tick(self):
        now = time.monotonic()
        self._schedule_periodic(now)

        # Refill only while the UART response window has room. This preserves
        # preemption for controls without giving up the pipeline.
        while self.queue and self.cb.can_accept_more():
            due, priority, seq, channel, can_id, payload, origin = self.queue[0]
            if due > time.monotonic():
                break
            heapq.heappop(self.queue)
            self._dispatch_one(channel, can_id, payload, origin)

    def next_deadline(self):
        deadlines = []
        if self.queue:
            deadlines.append(self.queue[0][0])
        for value in (
            self.next_display_zero if self.display_enabled else None,
            self.next_hu_heartbeat if self.hu_enabled else None,
            self.next_playback if self.playback in ("playing", "paused") else None,
            self.next_position if self.playback == "playing" else None,
        ):
            if value is not None:
                deadlines.append(value)
        return min(deadlines) if deadlines else None

    def snapshot(self):
        return {
            "display_enabled": self.display_enabled,
            "hu_enabled": self.hu_enabled,
            "hu_source": f"{self.hu_source:02X}",
            "playback": self.playback,
            "track": {
                "index": self.track_index,
                "artist": self.artist,
                "title": self.title,
                "text_mode": self.text_mode,
                "duration": self.duration,
                "position": self._current_position(),
                "allow_overflow": self.allow_overflow,
            },
            "queued_tx": len(self.queue),
        }


# ---------------------------------------------------------------------------
# GPIO character-device backend (/dev/gpiochip0).
# Uses the stable legacy line-handle ioctl ABI provided by Linux 6.6; no
# external libgpiod Python package is required.  This is preferred over
# direct /dev/mem register access on the Buildroot image.
# ---------------------------------------------------------------------------


class GPIOChipGPIO:
    GPIOHANDLES_MAX = 64

    GPIOHANDLE_REQUEST_INPUT = 1 << 0
    GPIOHANDLE_REQUEST_OUTPUT = 1 << 1
    GPIOHANDLE_REQUEST_ACTIVE_LOW = 1 << 2
    GPIOHANDLE_REQUEST_OPEN_DRAIN = 1 << 3
    GPIOHANDLE_REQUEST_OPEN_SOURCE = 1 << 4
    GPIOHANDLE_REQUEST_BIAS_PULL_UP = 1 << 5
    GPIOHANDLE_REQUEST_BIAS_PULL_DOWN = 1 << 6
    GPIOHANDLE_REQUEST_BIAS_DISABLE = 1 << 7

    _IOC_NRBITS = 8
    _IOC_TYPEBITS = 8
    _IOC_SIZEBITS = 14
    _IOC_DIRBITS = 2
    _IOC_NRSHIFT = 0
    _IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
    _IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
    _IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
    _IOC_WRITE = 1
    _IOC_READ = 2

    GPIOHANDLE_REQUEST_SIZE = 364
    GPIOHANDLE_DATA_SIZE = 64

    def __init__(self, logger, device="/dev/gpiochip0"):
        import fcntl
        self.fcntl = fcntl
        self.log = logger
        self.device = device
        self.chip_fd = None
        self.handles = {}
        self.handle_modes = {}
        self.group_handles = {}
        self.available = False
        self.backend = None

        self.GPIO_GET_LINEHANDLE_IOCTL = self._iowr(
            0xB4, 0x03, self.GPIOHANDLE_REQUEST_SIZE
        )
        self.GPIOHANDLE_GET_LINE_VALUES_IOCTL = self._iowr(
            0xB4, 0x08, self.GPIOHANDLE_DATA_SIZE
        )
        self.GPIOHANDLE_SET_LINE_VALUES_IOCTL = self._iowr(
            0xB4, 0x09, self.GPIOHANDLE_DATA_SIZE
        )

        try:
            self.chip_fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
            self.available = True
            self.backend = device
            self.log.write(
                "INFO",
                f"GPIO backend: {device} Linux line-handle API",
            )
        except Exception as exc:
            self.log.write(
                "WARN",
                f"GPIO disabled: cannot open {device}: {exc!r}",
            )
            self.close()

    @classmethod
    def _ioc(cls, direction, ioc_type, nr, size):
        return (
            (direction << cls._IOC_DIRSHIFT)
            | (ioc_type << cls._IOC_TYPESHIFT)
            | (nr << cls._IOC_NRSHIFT)
            | (size << cls._IOC_SIZESHIFT)
        )

    @classmethod
    def _iowr(cls, ioc_type, nr, size):
        return cls._ioc(cls._IOC_READ | cls._IOC_WRITE, ioc_type, nr, size)

    def _close_pin(self, pin):
        pin = int(pin)
        fd = self.handles.pop(pin, None)
        self.handle_modes.pop(pin, None)
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

    def close(self):
        for pin in list(self.handles):
            self._close_pin(pin)
        for key, fd in list(self.group_handles.items()):
            try:
                os.close(fd)
            except Exception:
                pass
        self.group_handles.clear()
        if self.chip_fd is not None:
            try:
                os.close(self.chip_fd)
            except Exception:
                pass
            self.chip_fd = None
        self.available = False
        self.backend = None

    def _request_line(self, pin, flags, default=0, mode=None):
        if not self.available:
            return None
        pin = int(pin)
        self._close_pin(pin)

        req = bytearray(self.GPIOHANDLE_REQUEST_SIZE)
        struct.pack_into("<I", req, 0, pin)
        struct.pack_into("<I", req, 256, int(flags))
        req[260] = 1 if default else 0
        label = b"peugeot-bridge"[:31]
        req[324:324 + len(label)] = label
        struct.pack_into("<I", req, 356, 1)
        struct.pack_into("<i", req, 360, -1)

        self.fcntl.ioctl(
            self.chip_fd, self.GPIO_GET_LINEHANDLE_IOCTL, req, True
        )
        line_fd = struct.unpack_from("<i", req, 360)[0]
        if line_fd < 0:
            raise OSError(f"GPIO line request returned invalid fd for GPIO{pin}")
        self.handles[pin] = line_fd
        self.handle_modes[pin] = mode
        return line_fd

    def request_input_group(self, pins, pull_up=True, label="peugeot-gpio-group"):
        if not self.available:
            return None
        pins = tuple(int(pin) for pin in pins)
        if not pins:
            raise ValueError("GPIO input group is empty")
        if len(pins) > self.GPIOHANDLES_MAX:
            raise ValueError("GPIO input group is too large")
        if len(set(pins)) != len(pins):
            raise ValueError("GPIO input group contains duplicates")

        # Close an older handle for the exact same group, if any.
        old = self.group_handles.pop(pins, None)
        if old is not None:
            try:
                os.close(old)
            except Exception:
                pass

        # A line cannot simultaneously belong to an individual and group handle.
        for pin in pins:
            if pin in self.handles:
                raise RuntimeError(f"GPIO{pin} is already requested individually")
        for other_pins in self.group_handles:
            overlap = set(pins).intersection(other_pins)
            if overlap:
                raise RuntimeError(
                    "GPIO input group overlaps existing group: "
                    + ",".join(str(v) for v in sorted(overlap))
                )

        req = bytearray(self.GPIOHANDLE_REQUEST_SIZE)
        for index, pin in enumerate(pins):
            struct.pack_into("<I", req, index * 4, pin)

        flags = self.GPIOHANDLE_REQUEST_INPUT
        if pull_up:
            flags |= self.GPIOHANDLE_REQUEST_BIAS_PULL_UP
        struct.pack_into("<I", req, 256, flags)

        encoded = str(label).encode("ascii", errors="replace")[:31]
        req[324:324 + len(encoded)] = encoded
        struct.pack_into("<I", req, 356, len(pins))
        struct.pack_into("<i", req, 360, -1)

        self.fcntl.ioctl(
            self.chip_fd, self.GPIO_GET_LINEHANDLE_IOCTL, req, True
        )
        line_fd = struct.unpack_from("<i", req, 360)[0]
        if line_fd < 0:
            raise OSError("GPIO group request returned invalid fd")

        self.group_handles[pins] = line_fd
        return pins

    def read_group(self, group_key):
        if not self.available:
            return [1] * len(group_key)
        key = tuple(group_key)
        fd = self.group_handles.get(key)
        if fd is None:
            raise RuntimeError("GPIO group has no requested line handle")
        data = bytearray(self.GPIOHANDLE_DATA_SIZE)
        self.fcntl.ioctl(
            fd, self.GPIOHANDLE_GET_LINE_VALUES_IOCTL, data, True
        )
        return [1 if data[i] else 0 for i in range(len(key))]

    def set_function(self, pin, function):
        # Compatibility shim used only by the optional BT open-drain path.
        # function=0 means input/high-Z; function=1 means output low/high
        # but callers should prefer set_input_pullup()/set_output().
        if int(function) == 0:
            self._request_line(
                pin, self.GPIOHANDLE_REQUEST_INPUT, mode="input"
            )
        elif int(function) == 1:
            self._request_line(
                pin, self.GPIOHANDLE_REQUEST_OUTPUT, default=0, mode="output"
            )
        else:
            raise ValueError("GPIOChipGPIO supports only input/output functions")

    def set_input_pullup(self, pin):
        if not self.available:
            return
        flags = (
            self.GPIOHANDLE_REQUEST_INPUT
            | self.GPIOHANDLE_REQUEST_BIAS_PULL_UP
        )
        self._request_line(pin, flags, mode="input_pullup")

    def set_output(self, pin, initial=False):
        if not self.available:
            return
        self._request_line(
            pin,
            self.GPIOHANDLE_REQUEST_OUTPUT,
            default=bool(initial),
            mode="output",
        )

    def _get_value(self, pin):
        fd = self.handles.get(int(pin))
        if fd is None:
            raise RuntimeError(f"GPIO{pin} has no requested line handle")
        data = bytearray(self.GPIOHANDLE_DATA_SIZE)
        self.fcntl.ioctl(
            fd, self.GPIOHANDLE_GET_LINE_VALUES_IOCTL, data, True
        )
        return 1 if data[0] else 0

    def _set_value(self, pin, value):
        fd = self.handles.get(int(pin))
        if fd is None:
            raise RuntimeError(f"GPIO{pin} has no requested line handle")
        data = bytearray(self.GPIOHANDLE_DATA_SIZE)
        data[0] = 1 if value else 0
        self.fcntl.ioctl(
            fd, self.GPIOHANDLE_SET_LINE_VALUES_IOCTL, data, True
        )

    def read(self, pin):
        if not self.available:
            return 1
        return self._get_value(pin)

    def write(self, pin, value):
        if not self.available:
            return
        self._set_value(pin, bool(value))

    def open_drain_press(self, pin, pressed):
        if not self.available:
            return
        pin = int(pin)
        if self.handle_modes.get(pin) != "open_drain":
            self._request_line(
                pin,
                self.GPIOHANDLE_REQUEST_OUTPUT
                | self.GPIOHANDLE_REQUEST_OPEN_DRAIN,
                default=1,
                mode="open_drain",
            )
        # Open-drain logical 0 drives low; logical 1 releases the line.
        self._set_value(pin, 0 if pressed else 1)


class JoystickGPIO:
    def __init__(self, gpio, scheduler, events, logger):
        self.gpio = gpio
        self.scheduler = scheduler
        self.events = events
        self.log = logger
        self.states = {}
        self.next_poll = time.monotonic()

        self.names = tuple(GPIO_JOYSTICK.keys())
        self.pins = tuple(GPIO_JOYSTICK[name] for name in self.names)
        self.group_key = None

        if self.gpio.available:
            self.group_key = self.gpio.request_input_group(
                self.pins, pull_up=True, label="peugeot-joystick"
            )
            raw_values = self.gpio.read_group(self.group_key)
            self.log.write(
                "INFO",
                "Joystick GPIO group: "
                + ", ".join(
                    f"{name}=GPIO{pin}"
                    for name, pin in zip(self.names, self.pins)
                ),
            )
        else:
            raw_values = [1] * len(self.pins)

        now = time.monotonic()
        for name, value in zip(self.names, raw_values):
            raw_pressed = not bool(value)
            self.states[name] = {
                "raw": raw_pressed,
                "stable": raw_pressed,
                "raw_since": now,
                "pressed_since": now if raw_pressed else None,
                "next_repeat": None,
            }

    def _emit(self, name, repeat=False):
        self.scheduler.display_button(name, origin="GPIO")
        self.events.semantic("gpio.joystick", button=name, pressed=True, repeat=repeat)

    def tick(self):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + GPIO_POLL_PERIOD

        if self.gpio.available and self.group_key is not None:
            raw_values = self.gpio.read_group(self.group_key)
        else:
            raw_values = [1] * len(self.pins)

        for name, value in zip(self.names, raw_values):
            st = self.states[name]
            raw = not bool(value)
            if raw != st["raw"]:
                st["raw"] = raw
                st["raw_since"] = now

            if raw != st["stable"] and now - st["raw_since"] >= GPIO_DEBOUNCE:
                st["stable"] = raw
                if raw:
                    st["pressed_since"] = now
                    st["next_repeat"] = now + JOYSTICK_REPEAT_DELAY
                    self._emit(name, repeat=False)
                else:
                    st["pressed_since"] = None
                    st["next_repeat"] = None
                    self.events.semantic("gpio.joystick", button=name, pressed=False, repeat=False)

            if st["stable"] and name in ("UP", "DOWN", "LEFT", "RIGHT"):
                nr = st["next_repeat"]
                if nr is not None and now >= nr:
                    self._emit(name, repeat=True)
                    held = now - st["pressed_since"]
                    interval = JOYSTICK_REPEAT_FAST if held >= JOYSTICK_REPEAT_FAST_AFTER else JOYSTICK_REPEAT_SLOW
                    st["next_repeat"] = now + interval

    def next_deadline(self):
        return self.next_poll

    def snapshot(self):
        return {
            name: {
                "bcm": GPIO_JOYSTICK[name],
                "pressed": bool(st["stable"]),
            }
            for name, st in self.states.items()
        }


class BluetoothGPIO:
    def __init__(self, gpio, events, logger):
        self.gpio = gpio
        self.events = events
        self.log = logger
        self.mode = BT_GPIO_MODE
        self.releases = []
        self.seq = 0

        if not self.gpio.available:
            return
        if self.mode == "active_high":
            for pin in GPIO_BT.values():
                self.gpio.set_output(pin, initial=False)
        elif self.mode == "open_drain_low":
            for pin in GPIO_BT.values():
                self.gpio.set_function(pin, 0)
        elif self.mode == "disabled":
            self.log.write(
                "INFO",
                "BT GPIO electrical driver disabled; pins reserved only",
            )
        else:
            raise ValueError(f"unknown BT_GPIO_MODE: {self.mode}")

    def press(self, button, origin="API"):
        button = str(button).upper()
        if button not in GPIO_BT:
            raise ValueError(f"unknown BT button: {button}")

        self.events.semantic("bt.button", button=button, origin=origin)
        if self.mode == "disabled" or not self.gpio.available:
            return False

        pin = GPIO_BT[button]
        if self.mode == "active_high":
            self.gpio.write(pin, True)
        elif self.mode == "open_drain_low":
            self.gpio.open_drain_press(pin, True)

        self.seq += 1
        heapq.heappush(self.releases, (time.monotonic() + BT_PULSE_SECONDS, self.seq, button))
        return True

    def tick(self):
        now = time.monotonic()
        while self.releases and self.releases[0][0] <= now:
            _, _, button = heapq.heappop(self.releases)
            pin = GPIO_BT[button]
            if self.mode == "active_high":
                self.gpio.write(pin, False)
            elif self.mode == "open_drain_low":
                self.gpio.open_drain_press(pin, False)

    def next_deadline(self):
        return self.releases[0][0] if self.releases else None

    def snapshot(self):
        return {
            "mode": self.mode,
            "pins": dict(GPIO_BT),
            "pending_releases": len(self.releases),
        }


# ---------------------------------------------------------------------------
# Harmony protocol/state
# ---------------------------------------------------------------------------

HARMONY_VOL_UP = bytes.fromhex("04 96 01 01 9C")
HARMONY_VOL_DOWN = bytes.fromhex("04 96 00 01 9B")
HARMONY_MUTE_TOGGLE = bytes.fromhex("02 8E 90")
HARMONY_DIRAC_TOGGLE = bytes.fromhex("02 B2 B4")
HARMONY_PRESET_CONFIRMED = {
    1: bytes.fromhex("03 8A 00 8D"),
    2: bytes.fromhex("03 8A 01 8E"),
}
HARMONY_REMOTE_PRESS = bytes.fromhex("03 97 01 9B")
HARMONY_REMOTE_RELEASE = bytes.fromhex("03 97 00 9A")
HARMONY_GROUP_SELECTOR = bytes.fromhex("03 8A 03 90")
HARMONY_GET_STATE_CANDIDATE = bytes.fromhex("00 02")  # old C-DSP protocol, unverified on Harmony

GROUP_MASTER = "master"
GROUP_SUB = "sub"
GROUP_UNKNOWN = "unknown"


def harmony_group_from_code(code):
    if code == 0x00:
        return GROUP_MASTER
    if code == 0x02:
        return GROUP_SUB
    return GROUP_UNKNOWN


def harmony_value_db(group, raw):
    if group == GROUP_MASTER:
        return -0.5 * int(raw)
    if group == GROUP_SUB:
        return (0x18 - int(raw)) * 0.5
    return None


class HarmonyController:
    """Harmony state cache plus minimal self-correcting volume transaction logic."""

    def __init__(self, scheduler, events, logger, status_leds):
        self.scheduler = scheduler
        self.events = events
        self.log = logger
        self.status_leds = status_leds

        self.active_group = GROUP_UNKNOWN
        self.master_raw = None
        self.sub_raw = None
        self.master_db = None
        self.sub_db = None
        self.mute = None
        self.preset = None
        self.dirac = None
        self.startup_value_raw = None
        self.last_state_mono = None

        self.volume_queue = deque()
        self.volume_op = None
        self.controls_enabled = AUTOCONTROL_DEFAULT

    def set_controls_enabled(self, enabled):
        self.controls_enabled = bool(enabled)
        self.events.semantic("control.enabled", enabled=self.controls_enabled)

    def _set_group(self, group, source):
        if group == GROUP_UNKNOWN:
            return
        changed = group != self.active_group
        self.active_group = group
        self.last_state_mono = time.monotonic()
        if changed:
            self.events.semantic("harmony.active_group", group=group, source=source)

    def _set_volume(self, group, raw, source):
        db = harmony_value_db(group, raw)
        if group == GROUP_MASTER:
            self.master_raw = raw
            self.master_db = db
        elif group == GROUP_SUB:
            self.sub_raw = raw
            self.sub_db = db
        self._set_group(group, source)
        self.events.semantic(
            "harmony.volume",
            group=group,
            raw=raw,
            db=db,
            source=source,
        )
        self.status_leds.synced()

    def handle_201(self, mono, payload):
        b = bytes(payload)
        if not b:
            return

        # Runtime volume: 05 86 GG VV 02 CS
        if len(b) == 6 and b[0] == 0x05 and b[1] == 0x86:
            group = harmony_group_from_code(b[2])
            if group != GROUP_UNKNOWN:
                self._set_volume(group, b[3], "0x86")
                self._volume_response(group)
            return

        # Active page/value: 06 8F 01 GG VV 00 CS
        if len(b) == 7 and b[0] == 0x06 and b[1] == 0x8F and b[2] == 0x01:
            group = harmony_group_from_code(b[3])
            if group != GROUP_UNKNOWN:
                self._set_volume(group, b[4], "0x8F")
                self._group_response(group, mono)
            return

        # Mute response: 04 8E state 02 CS; state 1=mute, 0=unmute.
        if len(b) == 5 and b[0] == 0x04 and b[1] == 0x8E:
            self.mute = bool(b[2])
            self.events.semantic("harmony.mute", enabled=self.mute)
            return

        # Dirac response: 04 B2 state 02 CS; observed 00=ON, 01=OFF.
        if len(b) == 5 and b[0] == 0x04 and b[1] == 0xB2:
            if b[2] in (0, 1):
                self.dirac = (b[2] == 0)
                self.events.semantic("harmony.dirac", enabled=self.dirac)
            return

        # Preset committed: 05 8A zero_based 00 02 CS.
        if len(b) == 6 and b[0] == 0x05 and b[1] == 0x8A:
            self.preset = int(b[2]) + 1
            self.events.semantic("harmony.preset", preset=self.preset)
            return

        # Startup main state: 07 84 VV 00 00 PP DD CS.
        if len(b) == 8 and b[0] == 0x07 and b[1] == 0x84:
            self.startup_value_raw = b[2]
            self.preset = int(b[5]) + 1
            self.dirac = (b[6] == 0)
            self.events.semantic(
                "harmony.startup_main",
                value_raw=b[2],
                preset=self.preset,
                dirac=self.dirac,
            )
            return

        # Startup active group: 05 B0 GG 01 0F CS.
        if len(b) == 6 and b[0] == 0x05 and b[1] == 0xB0:
            group = harmony_group_from_code(b[2])
            if group != GROUP_UNKNOWN:
                self._set_group(group, "startup-b0")
                if self.startup_value_raw is not None:
                    self._set_volume(group, self.startup_value_raw, "startup-0x84+B0")
            return

        # Wrapped startup form: 06 05 B0 GG 01 0F CS 00.
        if len(b) == 8 and b[0] == 0x06 and b[1] == 0x05 and b[2] == 0xB0:
            group = harmony_group_from_code(b[3])
            if group != GROUP_UNKNOWN:
                self._set_group(group, "startup-wrapper")
                if self.startup_value_raw is not None:
                    self._set_volume(group, self.startup_value_raw, "startup-wrapper")
            return

    def send_mute_toggle(self, origin="API"):
        self.scheduler.send_control(2, 0x202, HARMONY_MUTE_TOGGLE, f"{origin} HARMONY MUTE")

    def send_dirac_toggle(self, origin="API"):
        self.scheduler.send_control(2, 0x202, HARMONY_DIRAC_TOGGLE, f"{origin} HARMONY DIRAC")

    def send_preset(self, preset, origin="API"):
        preset = int(preset)
        if preset not in HARMONY_PRESET_CONFIRMED:
            raise ValueError("only Preset 1 and Preset 2 are confirmed for direct injection")
        self.scheduler.send_control(
            2, 0x202, HARMONY_PRESET_CONFIRMED[preset],
            f"{origin} HARMONY PRESET {preset}",
        )

    def send_get_state_candidate(self, origin="API"):
        self.scheduler.send_control(
            2, 0x202, HARMONY_GET_STATE_CANDIDATE,
            f"{origin} HARMONY GET-STATE CANDIDATE",
        )

    def request_volume(self, group, direction, origin="STEERING"):
        group = str(group).lower()
        if group not in (GROUP_MASTER, GROUP_SUB):
            raise ValueError("group must be master or sub")
        direction = 1 if int(direction) > 0 else -1
        if len(self.volume_queue) >= 64:
            self.log.write("WARN", "Harmony volume intent queue full; dropping step")
            return False
        self.volume_queue.append({
            "group": group,
            "direction": direction,
            "origin": origin,
            "created": time.monotonic(),
        })
        self.events.semantic(
            "harmony.volume_request",
            group=group,
            direction=direction,
            origin=origin,
        )
        return True

    def _step_payload(self, direction):
        return HARMONY_VOL_UP if direction > 0 else HARMONY_VOL_DOWN

    def _send_step(self, intent, phase="wait_step"):
        self.scheduler.send_control(
            2, 0x202, self._step_payload(intent["direction"]),
            f'{intent["origin"]} HARMONY {intent["group"].upper()} '
            f'{"+0.5" if intent["direction"] > 0 else "-0.5"}',
        )
        self.volume_op["phase"] = phase
        self.volume_op["deadline"] = time.monotonic() + 0.350

    def _send_group_toggle(self, phase):
        # OBSERVED sequence around Master->Sub selection on the real remote.
        # The reverse direction using the same selector command is still to be
        # verified in-car. We nevertheless validate the actual result via 0x8F
        # before any volume step and allow one corrective toggle only.
        now = time.monotonic()
        self.scheduler.send_control(2, 0x202, HARMONY_REMOTE_PRESS,
                                    "HARMONY GROUP TOGGLE PRESS", due=now)
        self.scheduler.send_control(2, 0x202, HARMONY_GROUP_SELECTOR,
                                    "HARMONY GROUP TOGGLE SELECT", due=now + 0.040)
        self.scheduler.send_control(2, 0x202, HARMONY_REMOTE_RELEASE,
                                    "HARMONY GROUP TOGGLE RELEASE", due=now + 0.080)
        self.volume_op["phase"] = phase
        self.volume_op["accept_group_after"] = now + 0.035
        self.volume_op["deadline"] = now + 0.600

    def _complete_op(self, result="ok"):
        if self.volume_op is not None:
            self.events.semantic(
                "harmony.volume_transaction",
                result=result,
                target=self.volume_op["intent"]["group"],
                direction=self.volume_op["intent"]["direction"],
            )
        self.volume_op = None

    def _fail_op(self, reason):
        self.log.write("WARN", f"Harmony volume transaction failed: {reason}")
        self._complete_op(result=reason)

    def _start_intent(self, intent):
        self.volume_op = {
            "intent": intent,
            "phase": "starting",
            "corrective_toggle_used": False,
            "rollback_group": None,
            "deadline": time.monotonic() + 0.500,
            "accept_group_after": 0.0,
        }

        if self.active_group == GROUP_UNKNOWN:
            # Normal power-up should have supplied startup B0 before controls.
            # Do not guess or toggle blind.
            self._fail_op("active_group_unknown")
            return

        if self.active_group == intent["group"]:
            self._send_step(intent, "wait_step")
        else:
            self._send_group_toggle("wait_toggle")

    def _group_response(self, group, mono):
        op = self.volume_op
        if op is None:
            return
        if op["phase"] not in ("wait_toggle", "wait_toggle_recover"):
            return
        if mono < op.get("accept_group_after", 0.0):
            return

        target = op["intent"]["group"]
        if group == target:
            final_phase = "wait_final_step" if op["phase"] == "wait_toggle_recover" else "wait_step"
            self._send_step(op["intent"], final_phase)
            return

        if not op["corrective_toggle_used"]:
            op["corrective_toggle_used"] = True
            self._send_group_toggle(op["phase"])
            return

        self._fail_op(f"toggle_confirmed_wrong_group:{group}")

    def _volume_response(self, group):
        op = self.volume_op
        if op is None:
            return
        phase = op["phase"]
        target = op["intent"]["group"]

        if phase in ("wait_step", "wait_final_step"):
            if group == target:
                self._complete_op("ok")
                return

            # Exactly one real misdirected step was observed. Roll back exactly
            # that one step and no history before it.
            op["rollback_group"] = group
            reverse = -op["intent"]["direction"]
            self.scheduler.send_control(
                2, 0x202, self._step_payload(reverse),
                "HARMONY ONE-STEP ROLLBACK",
            )
            op["phase"] = "wait_rollback"
            op["deadline"] = time.monotonic() + 0.350
            self.events.semantic(
                "harmony.volume_misdirected",
                intended=target,
                actual=group,
                rolled_back_direction=reverse,
            )
            return

        if phase == "wait_rollback":
            if group != op["rollback_group"]:
                self._fail_op(f"rollback_response_wrong_group:{group}")
                return
            self._send_group_toggle("wait_toggle_recover")

    def tick(self):
        now = time.monotonic()
        if self.volume_op is not None:
            if now > self.volume_op.get("deadline", now + 1):
                self._fail_op(f'timeout:{self.volume_op["phase"]}')
            return

        if self.volume_queue:
            intent = self.volume_queue.popleft()
            self._start_intent(intent)

    def snapshot(self):
        return {
            "controls_enabled": self.controls_enabled,
            "active_group": self.active_group,
            "master_raw": self.master_raw,
            "master_db": self.master_db,
            "sub_raw": self.sub_raw,
            "sub_db": self.sub_db,
            "mute": self.mute,
            "preset": self.preset,
            "dirac": self.dirac,
            "last_state_mono": self.last_state_mono,
            "queued_volume_steps": len(self.volume_queue),
            "volume_transaction": None if self.volume_op is None else {
                "phase": self.volume_op["phase"],
                "target": self.volume_op["intent"]["group"],
                "direction": self.volume_op["intent"]["direction"],
            },
        }


# ---------------------------------------------------------------------------
# Peugeot input decoder
# ---------------------------------------------------------------------------

STEERING_CODES = {
    bytes.fromhex("08 00 00"): "VOL_UP",
    bytes.fromhex("04 00 00"): "VOL_DOWN",
    bytes.fromhex("80 00 00"): "NEXT",
    bytes.fromhex("40 00 00"): "PREVIOUS",
    bytes.fromhex("02 00 00"): "PLAY_PAUSE",
    bytes.fromhex("00 FF 00"): "WHEEL_UP",
    bytes.fromhex("00 01 00"): "WHEEL_DOWN",
}
STEERING_RELEASE = bytes.fromhex("00 00 00")


class PeugeotInput:
    def __init__(self, harmony, bt, events, logger):
        self.harmony = harmony
        self.bt = bt
        self.events = events
        self.log = logger
        self.last_steering = None
        self.last_action_mono = 0.0

    def emulate_steering(self, action, origin="API"):
        action = str(action).upper()
        self._act(action, origin=origin, repeated=False)

    def _act(self, action, origin, repeated=False):
        self.events.semantic(
            "peugeot.steering",
            action=action,
            origin=origin,
            repeated=repeated,
        )

        if not self.harmony.controls_enabled and origin == "CAN":
            return

        if action == "VOL_UP":
            self.harmony.request_volume(GROUP_MASTER, +1, origin=origin)
        elif action == "VOL_DOWN":
            self.harmony.request_volume(GROUP_MASTER, -1, origin=origin)
        elif action == "WHEEL_UP":
            self.harmony.request_volume(GROUP_SUB, +1, origin=origin)
        elif action == "WHEEL_DOWN":
            self.harmony.request_volume(GROUP_SUB, -1, origin=origin)
        elif action == "NEXT":
            if not repeated:
                self.bt.press("NEXT", origin=origin)
        elif action == "PREVIOUS":
            if not repeated:
                self.bt.press("PREVIOUS", origin=origin)
        elif action == "PLAY_PAUSE":
            if not repeated:
                self.bt.press("PLAY_PAUSE", origin=origin)
        else:
            raise ValueError(f"unknown steering action: {action}")

    def handle_21f(self, mono, payload):
        data = bytes(payload)
        if data == STEERING_RELEASE:
            if self.last_steering is not None:
                self.events.semantic("peugeot.steering_release", action=self.last_steering)
            self.last_steering = None
            return

        action = STEERING_CODES.get(data)
        if action is None:
            self.events.semantic("peugeot.steering_unknown", data=data.hex().upper())
            return

        repeated = (action == self.last_steering)
        if repeated:
            # Volume/wheel holds may repeat, media commands are edge-only.
            if action in ("VOL_UP", "VOL_DOWN", "WHEEL_UP", "WHEEL_DOWN"):
                if mono - self.last_action_mono < 0.080:
                    return
            else:
                return

        self.last_steering = action
        self.last_action_mono = mono
        self._act(action, origin="CAN", repeated=repeated)

    def handle_3e5(self, mono, payload):
        data = bytes(payload)
        if data == DISPLAY_ZERO:
            self.events.semantic("peugeot.display_button", button="RELEASE", origin="CAN")
            return
        button = DISPLAY_BUTTONS_BY_DATA.get(data)
        if button is None:
            self.events.semantic("peugeot.display_button_unknown", data=data.hex().upper())
            return
        self.events.semantic("peugeot.display_button", button=button, origin="CAN")


class FrameRouter:
    def __init__(self, peugeot, harmony, events):
        self.peugeot = peugeot
        self.harmony = harmony
        self.events = events

    def handle_can(self, mono, frame):
        try:
            can_id = int(frame["id"].split(":", 1)[0], 16)
            payload = parse_hex_bytes(frame["data"])
        except Exception:
            return

        if frame["channel"] == 1:
            if can_id == 0x21F:
                self.peugeot.handle_21f(mono, payload)
            elif can_id == 0x3E5:
                self.peugeot.handle_3e5(mono, payload)

        elif frame["channel"] == 2:
            if can_id == 0x201:
                self.harmony.handle_201(mono, payload)
            elif can_id == 0x202:
                self.events.semantic(
                    "harmony.remote_command_seen",
                    data=payload.hex().upper(),
                )


# ---------------------------------------------------------------------------
# Hardware RX filter manager / debug sniff mode
# ---------------------------------------------------------------------------


class FilterManager:
    def __init__(self, cb, events, logger):
        self.cb = cb
        self.events = events
        self.log = logger
        self.debug_enabled = False
        self.applying = False
        self._generation = 0

    def _queue_batch(self, commands, target_debug):
        if self.applying:
            raise RuntimeError("filter reconfiguration already in progress")
        self.applying = True
        self._generation += 1
        generation = self._generation
        remaining = {"n": len(commands), "ok": True}

        def done(ok, payload, terminal):
            if generation != self._generation:
                return
            remaining["ok"] = remaining["ok"] and bool(ok)
            remaining["n"] -= 1
            if remaining["n"] == 0:
                self.applying = False
                if remaining["ok"]:
                    self.debug_enabled = bool(target_debug)
                    self.events.semantic(
                        "can.debug_filters",
                        enabled=self.debug_enabled,
                    )
                else:
                    self.log.write("ERR", "CAN filter reconfiguration failed", force_sync=True)

        for cmd in commands:
            if not self.cb.queue_command(cmd, "CAN FILTER CONFIG", callback=done):
                done(False, [], "queue_failed")

    def set_debug(self, enabled):
        enabled = bool(enabled)
        if enabled == self.debug_enabled and not self.applying:
            return

        if enabled:
            commands = [
                "CAN USER FILTER CH1 0 0000",
                "CAN USER MASK CH1 0000",
                "CAN USER FILTER CH2 0 0000",
                "CAN USER MASK CH2 0000",
            ]
        else:
            commands = [
                "CAN USER MASK CH1 07FF",
                "CAN USER FILTER CH1 0 021F",
                "CAN USER FILTER CH1 1 03E5",
                "CAN USER MASK CH2 07FF",
                "CAN USER FILTER CH2 0 0201",
                "CAN USER FILTER CH2 1 0202",
            ]
        self._queue_batch(commands, enabled)

    def snapshot(self):
        return {
            "debug_enabled": self.debug_enabled,
            "applying": self.applying,
            "production_rx_filters": {
                "CH1": [f"{x:03X}" for x in PRODUCTION_RX_FILTERS[1]],
                "CH2": [f"{x:03X}" for x in PRODUCTION_RX_FILTERS[2]],
            },
        }


# ---------------------------------------------------------------------------
# Local API
# ---------------------------------------------------------------------------


class ApiClient:
    MAX_INPUT = 65536
    MAX_OUTPUT = 262144

    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.closed = False
        self.sub_can = False
        self.sub_semantic = False
        self.channels = {1, 2}
        self.directions = {"rx", "tx"}
        self.dropped = 0

    def wants(self, event):
        if event.get("event") == "can":
            return (
                self.sub_can
                and event.get("channel") in self.channels
                and event.get("direction") in self.directions
            )
        if event.get("event") == "semantic":
            return self.sub_semantic
        return False


class ApiServer:
    def __init__(self, path, app, logger):
        self.path = Path(path)
        self.app = app
        self.log = logger
        self.clients = {}
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = os_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        os_socket.setblocking(False)
        os_socket.bind(str(self.path))
        os.chmod(self.path, 0o660)
        os_socket.listen(8)
        self.log.write("INFO", f"API listening on {self.path}", force_sync=True)

    def close(self):
        for client in list(self.clients.values()):
            self._close_client(client)
        try:
            self.server.close()
        except Exception:
            pass
        try:
            self.path.unlink()
        except Exception:
            pass

    def _close_client(self, client):
        if client.closed:
            return
        client.closed = True
        self.clients.pop(client.sock.fileno(), None)
        try:
            client.sock.close()
        except Exception:
            pass

    @staticmethod
    def _json_line(obj):
        return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def _queue_response(self, client, obj):
        data = self._json_line(obj)
        if len(client.outbuf) + len(data) > client.MAX_OUTPUT:
            self._close_client(client)
            return
        client.outbuf.extend(data)

    def _queue_event(self, client, event):
        if not client.wants(event):
            return
        data = self._json_line(event)
        if len(client.outbuf) + len(data) > client.MAX_OUTPUT:
            client.dropped += 1
            return
        if client.dropped:
            notice = self._json_line({"event":"dropped","mono":time.monotonic(),"count":client.dropped})
            if len(client.outbuf) + len(notice) + len(data) <= client.MAX_OUTPUT:
                client.outbuf.extend(notice)
                client.dropped = 0
            else:
                client.dropped += 1
                return
        client.outbuf.extend(data)

    def publish(self, event):
        for client in list(self.clients.values()):
            if not client.closed:
                self._queue_event(client, event)

    def _accept_all(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except BlockingIOError:
                return
            sock.setblocking(False)
            client = ApiClient(sock)
            self.clients[sock.fileno()] = client
            self._queue_response(client, {
                "ok": True,
                "hello": "peugeot-bridge",
                "api_version": 2,
                "bridge_version": DEV_VERSION,
            })

    def _handle_request(self, client, request):
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        cmd = str(request.get("cmd", "")).strip().lower()
        if not cmd:
            raise ValueError("missing cmd")

        if cmd == "status":
            return {"ok": True, "state": self.app.snapshot()}

        if cmd == "subscribe":
            client.sub_can = bool(request.get("can", False))
            client.sub_semantic = bool(request.get("semantic", False))
            channels = request.get("channels", [1, 2])
            directions = request.get("directions", ["rx", "tx"])
            client.channels = {int(v) for v in channels if int(v) in (1, 2)}
            client.directions = {str(v).lower() for v in directions if str(v).lower() in ("rx", "tx")}
            if not client.channels:
                raise ValueError("subscription has no valid channels")
            if client.sub_can and not client.directions:
                raise ValueError("CAN subscription has no valid directions")
            return {"ok": True, "subscription": {
                "can": client.sub_can,
                "semantic": client.sub_semantic,
                "channels": sorted(client.channels),
                "directions": sorted(client.directions),
            }}

        if cmd == "control.set":
            self.app.harmony.set_controls_enabled(request.get("enabled", True))
            return {"ok": True, "enabled": self.app.harmony.controls_enabled}

        if cmd == "debug.set":
            self.app.filters.set_debug(request.get("enabled", True))
            return {"ok": True, "queued": True, "enabled": bool(request.get("enabled", True))}

        if cmd == "display.enable":
            self.app.scheduler.display_enable(request.get("enabled", True))
            return {"ok": True}

        if cmd == "display.button":
            self.app.scheduler.display_button(request.get("button", ""), origin="API")
            return {"ok": True}

        if cmd == "steering.button":
            self.app.peugeot.emulate_steering(request.get("button", ""), origin="API")
            return {"ok": True}

        if cmd == "media.button":
            physical = self.app.bt.press(request.get("button", ""), origin="API")
            return {"ok": True, "gpio_activated": physical}

        if cmd == "harmony.volume":
            group = request.get("group", "master")
            direction = request.get("direction", 1)
            self.app.harmony.request_volume(group, direction, origin="API")
            return {"ok": True, "queued": True}

        if cmd == "harmony.mute":
            self.app.harmony.send_mute_toggle("API")
            return {"ok": True}

        if cmd == "harmony.dirac":
            self.app.harmony.send_dirac_toggle("API")
            return {"ok": True}

        if cmd == "harmony.preset":
            self.app.harmony.send_preset(request.get("preset"), "API")
            return {"ok": True}

        if cmd == "harmony.get_state_test":
            self.app.harmony.send_get_state_candidate("API")
            return {"ok": True, "warning": "0x202 00 02 is not yet verified on Harmony"}

        if cmd == "hu.enable":
            self.app.scheduler.hu_enable(request.get("enabled", True))
            return {"ok": True}

        if cmd == "hu.source_raw":
            self.app.scheduler.hu_source_raw(request.get("value"))
            return {"ok": True, "source": f"{self.app.scheduler.hu_source:02X}"}

        if cmd == "track.set":
            self.app.scheduler.set_track(
                artist=request.get("artist", ""),
                title=request.get("title", ""),
                duration=request.get("duration"),
                position=request.get("position", 0),
                track_index=request.get("track_index", 1),
                text_mode=request.get("text_mode", "translit"),
                allow_overflow=request.get("allow_overflow", False),
                playback=request.get("playback"),
            )
            return {"ok": True}

        if cmd == "track.position":
            if "position" not in request:
                raise ValueError("track.position requires position")
            self.app.scheduler.set_position(
                request["position"],
                allow_overflow=request.get("allow_overflow"),
            )
            return {"ok": True}

        if cmd == "playback.set":
            self.app.scheduler.set_playback(request.get("state", ""))
            return {"ok": True}

        raise ValueError(f"unknown cmd: {cmd}")

    def _read_client(self, client):
        try:
            data = client.sock.recv(4096)
        except BlockingIOError:
            return
        if not data:
            self._close_client(client)
            return
        client.inbuf.extend(data)
        if len(client.inbuf) > client.MAX_INPUT:
            self._queue_response(client, {"ok": False, "error": "request too large"})
            self._close_client(client)
            return
        while True:
            pos = client.inbuf.find(b"\n")
            if pos < 0:
                return
            raw = bytes(client.inbuf[:pos])
            del client.inbuf[:pos + 1]
            if not raw.strip():
                continue
            try:
                request = json.loads(raw.decode("utf-8"))
                response = self._handle_request(client, request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            if not client.closed:
                self._queue_response(client, response)

    def _write_client(self, client):
        if not client.outbuf:
            return
        try:
            sent = client.sock.send(client.outbuf)
        except BlockingIOError:
            return
        except (BrokenPipeError, ConnectionResetError):
            self._close_client(client)
            return
        if sent > 0:
            del client.outbuf[:sent]

    def poll(self, timeout):
        read_fds = [self.server]
        write_fds = []
        for client in list(self.clients.values()):
            if client.closed:
                continue
            read_fds.append(client.sock)
            if client.outbuf:
                write_fds.append(client.sock)
        try:
            readable, writable, _ = select.select(
                read_fds, write_fds, [], max(0.0, float(timeout)))
        except (OSError, ValueError):
            return
        if self.server in readable:
            self._accept_all()
            readable = [fd for fd in readable if fd is not self.server]
        for sock in readable:
            client = self.clients.get(sock.fileno())
            if client is not None:
                self._read_client(client)
        for sock in writable:
            client = self.clients.get(sock.fileno())
            if client is not None:
                self._write_client(client)


class BridgeApp:
    def __init__(self, cb, scheduler, harmony, peugeot, filters, gpio, joystick, bt, status):
        self.cb = cb
        self.scheduler = scheduler
        self.harmony = harmony
        self.peugeot = peugeot
        self.filters = filters
        self.gpio = gpio
        self.joystick = joystick
        self.bt = bt
        self.status = status

    def snapshot(self):
        return {
            "version": DEV_VERSION,
            "uart": self.cb.snapshot(),
            "scheduler": self.scheduler.snapshot(),
            "harmony": self.harmony.snapshot(),
            "filters": self.filters.snapshot(),
            "gpio": {
                "available": self.gpio.available,
                "joystick": self.joystick.snapshot(),
                "bluetooth": self.bt.snapshot(),
            },
            "status_led_mode": self.status.mode,
            "tx_whitelist": {
                "CH1": [f"{x:03X}" for x in sorted(TX_ALLOW[1])],
                "CH2": [f"{x:03X}" for x in sorted(TX_ALLOW[2])],
            },
        }


def configure_production_can(cb, log):
    commands = [
        "CAN MODE USER",
        "CAN USER ALIGN RIGHT",
        "CAN USER OPEN CH1 125K",
        "CAN USER MASK CH1 07FF",
        "CAN USER FILTER CH1 0 021F",
        "CAN USER FILTER CH1 1 03E5",
        "CAN USER OPEN CH2 250K",
        "CAN USER MASK CH2 07FF",
        "CAN USER FILTER CH2 0 0201",
        "CAN USER FILTER CH2 1 0202",
    ]
    for command in commands:
        ok, payload = cb.command_sync(command, timeout=1.5)
        if not ok:
            raise RuntimeError(f"CarBerry setup failed: {command} payload={payload}")


def compute_wait(*deadlines, maximum=0.005):
    now = time.monotonic()
    waits = [maximum]
    for deadline in deadlines:
        if deadline is not None:
            waits.append(max(0.0, deadline - now))
    return min(waits)


def main():
    log = BridgeLog()
    cb = None
    api = None
    gpio = None
    status = None

    try:
        events = EventHub(log)
        cb = CarBerryUART(log, can_rx_callback=events.can_rx)
        status = StatusLEDs(cb)

        # Setup is intentionally synchronous. Continuous runtime CAN TX is not.
        configure_production_can(cb, log)
        status.can_ready()

        scheduler = TxScheduler(cb, events, log)
        gpio = GPIOChipGPIO(log)
        bt = BluetoothGPIO(gpio, events, log)
        harmony = HarmonyController(scheduler, events, log, status)
        peugeot = PeugeotInput(harmony, bt, events, log)
        joystick = JoystickGPIO(gpio, scheduler, events, log)
        filters = FilterManager(cb, events, log)
        router = FrameRouter(peugeot, harmony, events)
        events.attach_router(router)

        app = BridgeApp(cb, scheduler, harmony, peugeot, filters, gpio, joystick, bt, status)
        api = ApiServer(API_SOCKET, app, log)
        events.attach_api(api)

        log.write(
            "INFO",
            "BRIDGE READY: direct UART + pipelined TX + production RX filters",
            force_sync=True,
        )
        print(f"===== PEUGEOT / CARBERRY BRIDGE {DEV_VERSION} =====", flush=True)
        print("UART: /dev/ttyAMA0 direct; carberry_d must NOT be running", flush=True)
        print("CH1: Peugeot 125K RX whitelist 021F,03E5", flush=True)
        print("CH2: Harmony 250K RX whitelist 0201,0202", flush=True)
        print(f"Autocontrol default: {AUTOCONTROL_DEFAULT}", flush=True)
        print(f"API: {API_SOCKET}", flush=True)

        while not stop_requested:
            # Service UART aggressively; scheduler refills an 8-command window.
            cb.pump()
            harmony.tick()
            joystick.tick()
            bt.tick()
            scheduler.tick()
            status.tick()
            cb.pump()

            wait = compute_wait(
                scheduler.next_deadline(),
                joystick.next_deadline(),
                bt.next_deadline(),
                status.next_deadline(),
                maximum=0.005,
            )
            api.poll(wait)

    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        log.write("FATAL", repr(exc), force_sync=True)
        if status is not None:
            try:
                status.error()
                for _ in range(20):
                    if cb is not None:
                        cb.pump()
                    time.sleep(0.005)
            except Exception:
                pass
        return 1

    finally:
        if api is not None:
            api.close()

        # Best-effort LED clear while we still own the UART. Only do this when
        # the runtime queue has drained enough to preserve response ordering.
        if cb is not None:
            try:
                deadline = time.monotonic() + 0.250
                while (cb.pending or cb.txbuf) and time.monotonic() < deadline:
                    cb.pump()
                    time.sleep(0.002)
                if not cb.pending and not cb.txbuf:
                    cb.command_sync("GPLED LED1 CLEAR", timeout=0.5, quiet=True)
                    cb.command_sync("GPLED LED2 CLEAR", timeout=0.5, quiet=True)
            except Exception:
                pass
            cb.close()

        if gpio is not None:
            gpio.close()
        log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
