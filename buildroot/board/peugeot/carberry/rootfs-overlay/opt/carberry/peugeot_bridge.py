#!/usr/bin/env python3

import heapq
import json
import os
import re
import select
import signal
import socket
import sys
import time
from collections import deque
from pathlib import Path


HOST = "127.0.0.1"
PORT = 7070
API_SOCKET = Path("/run/peugeot-bridge.sock")

# Permanent project wiring. Never swap these channels.
CHANNELS = {
    "CH1": ("PEUGEOT", "125K"),
    "CH2": ("HARMONY", "250K"),
}

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


class BridgeLog:
    """
    Operational log only.

    S35peugeot-bridge redirects stdout/stderr to /var/log/carberry/bridge.log.
    Raw CAN capture is deliberately NOT owned by the bridge. A separate
    subscriber (peugeot-sniffer) receives CAN events over the local API and
    decides where/how to store them.
    """

    def write(self, direction, text, force_sync=False):
        mono = time.monotonic()
        print(
            f"{mono:14.6f}  {direction:<5}  {text}",
            flush=True,
        )

    def close(self):
        pass


def fresh_socket_command(sock, command, timeout=1.5):
    """
    Command helper independent of the main stop_requested flag.
    Used for final LED cleanup during graceful shutdown.
    """
    sock.sendall(command.encode("ascii") + b"\r")

    buf = bytearray()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)

            if not data:
                return False

            buf.extend(data)

            while True:
                pos = buf.find(b"\r\n")
                if pos < 0:
                    break

                raw = bytes(buf[:pos])
                del buf[:pos + 2]
                line = raw.decode("ascii", errors="backslashreplace")

                if line == "OK":
                    return True

                if line.startswith("ERROR"):
                    return False

        except socket.timeout:
            pass

    return False


def clear_status_leds_best_effort(logger=None):
    """Clear both CarBerry GPLEDs while carberry_d is still alive."""
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5) as sock:
            sock.settimeout(0.20)

            green_ok = fresh_socket_command(sock, "GPLED LED1 CLEAR")
            red_ok = fresh_socket_command(sock, "GPLED LED2 CLEAR")

            if logger is not None:
                logger.write(
                    "INFO",
                    "STATUS LED shutdown clear: "
                    f"green={green_ok} red={red_ok}",
                    force_sync=True,
                )

            return green_ok and red_ok

    except Exception as exc:
        if logger is not None:
            logger.write(
                "WARN",
                f"STATUS LED shutdown clear failed: {exc!r}",
                force_sync=True,
            )
        return False


def wait_for_carberry():
    """
    Wait until TCP accepts connections AND the PIC command processor replies.

    TCP :7070 being open alone is not sufficient. carberry_d can accept TCP
    before the PIC side is ready.
    """
    print("Waiting for CarBerry command interface...", flush=True)

    while not stop_requested:
        sock = None

        try:
            sock = socket.create_connection((HOST, PORT), timeout=2.0)
            sock.settimeout(0.20)

            # carberry_d can wait about five seconds on the PIC UART. Keep this
            # client alive longer than that to avoid abandoned overlapping
            # requests when the PIC is absent/not ready.
            if fresh_socket_command(sock, "CAN MODE", timeout=6.0):
                print("CarBerry command interface ready.", flush=True)
                return True

        except (OSError, ConnectionError):
            pass

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        for _ in range(10):
            if stop_requested:
                return False
            time.sleep(0.1)

    return False


# CarBerry USER receive format with RIGHT alignment, per CarBerry wiki:
#   RX1 03E5-000010000000
#   RX2 0201-...
# Optional CarBerry hardware timestamp is accepted as well.
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


class CarBerry:
    def __init__(self, logger, can_rx_callback=None):
        self.log = logger
        self.buf = bytearray()
        self.can_rx_callback = can_rx_callback

        self.sock = socket.create_connection((HOST, PORT), timeout=3.0)
        self.sock.settimeout(0.05)

        self.log.write(
            "INFO",
            f"TCP connected to {HOST}:{PORT}",
            force_sync=True,
        )

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def set_can_rx_callback(self, callback):
        self.can_rx_callback = callback

    def _route_async_line(self, line, mono=None):
        frame = parse_can_rx(line)
        if frame is None:
            return False

        if mono is None:
            mono = time.monotonic()

        if self.can_rx_callback is not None:
            self.can_rx_callback(mono, frame)

        return True

    def _extract_line(self):
        pos = self.buf.find(b"\r\n")
        if pos < 0:
            return None

        raw = bytes(self.buf[:pos])
        del self.buf[:pos + 2]
        return raw.decode("ascii", errors="backslashreplace")

    def _read_line(self, deadline=None):
        while not stop_requested:
            line = self._extract_line()
            if line is not None:
                return line

            if deadline is not None and time.monotonic() >= deadline:
                return None

            try:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("CarBerry daemon closed connection")
                self.buf.extend(data)
            except socket.timeout:
                pass

        return None

    def command(self, command, timeout=3.0, quiet=False):
        if not quiet:
            print(f">>> {command}", flush=True)

        if not quiet:
            self.log.write("CMD", command)
        self.sock.sendall(command.encode("ascii") + b"\r")

        deadline = time.monotonic() + timeout
        payload = []

        while not stop_requested:
            line = self._read_line(deadline)

            if line is None:
                if stop_requested:
                    return False, payload

                self.log.write(
                    "ERR",
                    f"COMMAND TIMEOUT: {command}",
                    force_sync=True,
                )
                if not quiet:
                    print(
                        f"TIMEOUT: {command}",
                        file=sys.stderr,
                        flush=True,
                    )
                return False, payload

            # CAN traffic can arrive while carberry_d is serving a command.
            # Route it instead of swallowing it as command output.
            if self._route_async_line(line):
                continue

            if not quiet or line.startswith("ERROR"):
                self.log.write("RX", line)

            if not quiet:
                print(f"<<< {line}", flush=True)

            if line == "OK":
                return True, payload

            if line.startswith("ERROR"):
                return False, payload

            payload.append(line)

        return False, payload

    def drain_async(self, max_lines=512, max_reads=16):
        """
        Drain a bounded amount of asynchronous CarBerry output.

        The bound prevents a permanently busy CAN bus from starving API and
        scheduler work. Remaining socket data is handled on the next loop.
        """
        lines = 0
        reads = 0

        while not stop_requested and lines < max_lines and reads < max_reads:
            line = self._extract_line()
            if line is not None:
                mono = time.monotonic()
                if not self._route_async_line(line, mono):
                    self.log.write("RX", line)
                lines += 1
                continue

            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return

            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return

            if not data:
                raise ConnectionError("CarBerry daemon closed connection")

            self.buf.extend(data)
            reads += 1


class StatusLEDs:
    """
    CarBerry HW 1.00 / FW 1.19:
      LED1 = GREEN
      LED2 = RED

    Early boot RED is produced by S00carberry-boot-led over direct UART.
    After the bridge confirms the PIC API, LED control is TCP-only.
    """

    GREEN = "LED1"
    RED = "LED2"

    PATTERN_INITIALIZING = (
        (True, 0.250),
        (False, 0.750),
    )
    PATTERN_UNSYNCED = (
        (True, 0.150),
        (False, 1.850),
    )
    PATTERN_SYNCED = (
        (True, 0.150),
        (False, 0.150),
        (True, 0.150),
        (False, 1.550),
    )
    PATTERN_ERROR = (
        (True, 0.125),
        (False, 0.125),
    )

    def __init__(self, cb):
        self.cb = cb
        self.enabled = True
        self.green = None
        self.red = None
        self.mode = "unknown"
        self.volume_synced = False
        self.pattern_led = None
        self.pattern = ()
        self.pattern_index = 0
        self.next_transition = 0.0

    def _set(self, led, on):
        if not self.enabled:
            return False

        current = self.green if led == self.GREEN else self.red
        if current is on:
            return True

        action = "SET" if on else "CLEAR"

        try:
            ok, _ = self.cb.command(
                f"GPLED {led} {action}",
                timeout=1.0,
                quiet=True,
            )
        except Exception as exc:
            self.cb.log.write(
                "WARN",
                f"STATUS LED EXCEPTION: {led} {action}: {exc!r}",
                force_sync=True,
            )
            self.enabled = False
            return False

        if not ok:
            self.cb.log.write(
                "WARN",
                f"STATUS LED COMMAND FAILED: {led} {action}",
                force_sync=True,
            )
            self.enabled = False
            return False

        if led == self.GREEN:
            self.green = on
        else:
            self.red = on

        return True

    def _stop_pattern(self):
        self.pattern_led = None
        self.pattern = ()
        self.pattern_index = 0
        self.next_transition = 0.0

    def _start_pattern(self, led, pattern):
        self.pattern_led = led
        self.pattern = pattern
        self.pattern_index = 0
        state, duration = self.pattern[0]
        if self._set(led, state):
            self.next_transition = time.monotonic() + duration

    def initializing(self):
        self.mode = "initializing"
        self.volume_synced = False
        self._stop_pattern()
        self._set(self.GREEN, False)
        self._start_pattern(self.RED, self.PATTERN_INITIALIZING)
        self.cb.log.write(
            "INFO",
            "STATUS: RED heartbeat - CarBerry API ready, CAN initializing",
        )

    def can_ready(self):
        self.mode = "can_ready"
        self.volume_synced = False
        self._stop_pattern()
        self._set(self.RED, False)
        self._set(self.GREEN, False)
        self._start_pattern(self.GREEN, self.PATTERN_UNSYNCED)
        self.cb.log.write(
            "INFO",
            "STATUS: GREEN single heartbeat - CAN ready, synchronization not confirmed",
            force_sync=True,
        )

    def set_volume_synced(self, synced=True):
        self.volume_synced = bool(synced)
        if not self.volume_synced:
            self.can_ready()
            return

        self.mode = "synced"
        self._stop_pattern()
        self._set(self.RED, False)
        self._set(self.GREEN, False)
        self._start_pattern(self.GREEN, self.PATTERN_SYNCED)
        self.cb.log.write(
            "INFO",
            "STATUS: GREEN double heartbeat - synchronized",
            force_sync=True,
        )

    def error(self):
        self.mode = "error"
        self.volume_synced = False
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
        if self._set(self.pattern_led, state):
            self.next_transition = time.monotonic() + duration

    def next_deadline(self):
        if not self.enabled or self.pattern_led is None:
            return None
        return self.next_transition


def require(cb, status, command):
    ok, _ = cb.command(command)
    status.tick()

    if not ok:
        cb.log.write(
            "ERR",
            f"COMMAND FAILED: {command}",
            force_sync=True,
        )
        print(
            f"ERROR: command failed: {command}",
            file=sys.stderr,
            flush=True,
        )

    return ok


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
DISPLAY_ZERO = bytes(6)


RU_TRANSLIT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D",
    "Е": "E", "Ё": "Yo", "Ж": "Zh", "З": "Z", "И": "I",
    "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N",
    "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
    "У": "U", "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch",
    "Ш": "Sh", "Щ": "Sch", "Ъ": "", "Ы": "Y", "Ь": "",
    "Э": "E", "Ю": "Yu", "Я": "Ya",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def transliterate_ru(text):
    return "".join(RU_TRANSLIT.get(ch, ch) for ch in text)


def encode_display_text(text, mode="translit"):
    """Encode text to one-byte display data. Diagnostic modes are explicit."""
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
    """
    AM2 working MP3 metadata sequence for Peugeot 0x0A4.

    40 display bytes = artist[20] + title[20].
    First frame carries two text bytes, then 0x21..0x26 continuations.
    """
    artist_bytes = encode_display_text(artist, text_mode)[:20].ljust(20, b"\x00")
    title_bytes = encode_display_text(title, text_mode)[:20].ljust(20, b"\x00")
    text = artist_bytes + title_bytes

    frames = [
        bytes((0x10, 0x2C, 0x20, 0x00, 0x98, track_index & 0xFF)) + text[:2]
    ]

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

    return bytes((
        track_index & 0xFF,
        total_min,
        total_sec,
        pos_min,
        pos_sec,
        0x00,
    ))


class EventHub:
    def __init__(self, logger):
        self.log = logger
        self.api = None

    def attach_api(self, api):
        self.api = api

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

    def can_tx(self, mono, channel, can_id, payload, origin):
        event = {
            "event": "can",
            "mono": mono,
            "direction": "tx",
            "channel": channel,
            "id": f"{can_id:04X}",
            "data": payload.hex().upper(),
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


class TxScheduler:
    DISPLAY_ZERO_PERIOD = 0.500
    HU_HEARTBEAT_PERIOD = 0.100
    PLAYBACK_PERIOD = 0.500
    POSITION_PERIOD = 1.000

    def __init__(self, cb, events, logger):
        self.cb = cb
        self.events = events
        self.log = logger

        self.queue = []
        self.queue_seq = 0

        self.display_enabled = False
        self.next_display_zero = None

        self.hu_enabled = False
        # Current Chinese HU/box capture used 0x165 C0 C4 20 00 at ~100 ms
        # and produced the desired Bluetooth/phone source icon. dev03 keeps
        # that header shape and exposes only byte #2 for the C0..CF scan.
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

    def _enqueue(self, channel, can_id, payload, origin, due=None):
        if due is None:
            due = time.monotonic()

        payload = bytes(payload)
        if not 0 <= can_id <= 0x7FF:
            raise ValueError("only standard 11-bit CAN IDs are supported here")
        if len(payload) > 8:
            raise ValueError("CAN payload exceeds 8 bytes")
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")

        self.queue_seq += 1
        heapq.heappush(
            self.queue,
            (float(due), self.queue_seq, channel, can_id, payload, origin),
        )

    def _send(self, channel, can_id, payload, origin):
        command = (
            f"CAN USER TX CH{channel} {can_id:04X} "
            f"{payload.hex().upper()}"
        )

        mono = time.monotonic()
        self.events.can_tx(mono, channel, can_id, payload, origin)
        ok, response = self.cb.command(command, timeout=1.0, quiet=True)

        if not ok:
            self.log.write(
                "ERR",
                f"CAN TX FAILED origin={origin} command={command} response={response}",
                force_sync=True,
            )
            self.events.semantic(
                "can.tx_failed",
                origin=origin,
                channel=channel,
                can_id=f"{can_id:04X}",
                data=payload.hex().upper(),
            )

        return ok

    def display_enable(self, enabled):
        self.display_enabled = bool(enabled)
        self.next_display_zero = (
            time.monotonic() if self.display_enabled else None
        )
        self.events.semantic("display.enabled", enabled=self.display_enabled)

    def display_button(self, button):
        button = str(button).upper()
        if button not in DISPLAY_BUTTONS:
            raise ValueError(f"unknown DISPLAY button: {button}")

        now = time.monotonic()
        self._enqueue(
            1,
            0x3E5,
            DISPLAY_BUTTONS[button],
            f"DISPLAY BUTTON {button}",
            now,
        )

        # If periodic DISPLAY ZERO is not enabled, still guarantee a release.
        # This is a safety fallback for one-shot/manual use. When enabled, the
        # normal 500 ms cadence supplies the release just like old AM2.
        if not self.display_enabled:
            self._enqueue(
                1,
                0x3E5,
                DISPLAY_ZERO,
                "DISPLAY RELEASE FALLBACK",
                now + self.DISPLAY_ZERO_PERIOD,
            )

        self.events.semantic("display.button", button=button)

    def hu_enable(self, enabled):
        self.hu_enabled = bool(enabled)
        self.next_hu_heartbeat = (
            time.monotonic() if self.hu_enabled else None
        )
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

        self.events.semantic(
            "hu.source_raw",
            old=f"{old:02X}",
            value=f"{self.hu_source:02X}",
        )

    def _current_position(self, now=None):
        if now is None:
            now = time.monotonic()

        position = self.position_base
        if self.playback == "playing":
            position += max(0.0, now - self.position_base_mono)

        position = int(position)
        if (
            not self.allow_overflow
            and self.duration is not None
            and position > self.duration
        ):
            position = int(self.duration)

        return max(0, position)

    def set_position(self, position, allow_overflow=None):
        position = max(0, int(position))
        if allow_overflow is not None:
            self.allow_overflow = bool(allow_overflow)

        self.position_base = float(position)
        self.position_base_mono = time.monotonic()

        self._enqueue(
            1,
            0x3A5,
            encode_track_position(
                self.track_index,
                self.duration,
                self._current_position(),
            ),
            "TRACK POSITION SET",
        )

        if self.playback == "playing":
            self.next_position = time.monotonic() + self.POSITION_PERIOD

        self.events.semantic(
            "track.position",
            position=position,
            allow_overflow=self.allow_overflow,
        )

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
            self.next_position = (
                now if state == "playing" else None
            )

        self.events.semantic("playback.state", state=state)

    def set_track(
        self,
        artist,
        title,
        duration=None,
        position=0,
        track_index=1,
        text_mode="translit",
        allow_overflow=False,
        playback=None,
    ):
        self.artist = "" if artist is None else str(artist)
        self.title = "" if title is None else str(title)
        self.track_index = int(track_index) & 0xFF
        if self.track_index == 0:
            self.track_index = 1

        self.text_mode = str(text_mode)
        # Validate encoding now, before touching state further.
        encode_display_text(self.artist, self.text_mode)
        encode_display_text(self.title, self.text_mode)

        self.duration = None if duration is None else max(0, int(duration))
        self.allow_overflow = bool(allow_overflow)
        self.position_base = float(max(0, int(position)))
        self.position_base_mono = time.monotonic()

        now = time.monotonic()
        spacing = 0.003
        step = 0

        # Sequence reconstructed from the working AM2 Android->Arduino path.
        # 0x365 is intentionally omitted: final Arduino whitelist did not pass
        # it to CAN, while track display still worked.
        self._enqueue(
            1,
            0x0A4,
            bytes((0x04, 0x00, 0x00, 0x00, self.track_index)),
            "TRACK PREAMBLE",
            now + step * spacing,
        )
        step += 1

        self._enqueue(
            1,
            0x3A5,
            bytes((self.track_index, 0xFF, 0xFF, 0x00, 0x80, 0x80)),
            "TRACK NO-POSITION",
            now + step * spacing,
        )
        step += 1

        # Old Android emitted a one-shot PLAY state on track change before
        # sending the text sequence. 0x365 was also attempted there, but the
        # final Arduino whitelist dropped 0x365, so it is intentionally absent.
        self._enqueue(
            1,
            0x325,
            bytes((0x00, 0x0B, 0x00)),
            "TRACK PRE-PLAY",
            now + step * spacing,
        )
        step += 1

        for frame in encode_track_metadata(
            self.track_index,
            self.artist,
            self.title,
            self.text_mode,
        ):
            self._enqueue(
                1,
                0x0A4,
                frame,
                "TRACK METADATA",
                now + step * spacing,
            )
            step += 1

        self.events.semantic(
            "track.set",
            artist=self.artist,
            title=self.title,
            duration=self.duration,
            position=int(self.position_base),
            track_index=self.track_index,
            text_mode=self.text_mode,
            allow_overflow=self.allow_overflow,
        )

        sequence_end = now + step * spacing

        if playback is not None:
            self.set_playback(playback)
            # Do not let periodic status/position frames interleave with the
            # finite metadata sequence. They start immediately after it.
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
                self._enqueue(
                    1,
                    0x3E5,
                    DISPLAY_ZERO,
                    "DISPLAY ZERO",
                    now,
                )
                while self.next_display_zero <= now:
                    self.next_display_zero += self.DISPLAY_ZERO_PERIOD

        if self.hu_enabled:
            if self.next_hu_heartbeat is None:
                self.next_hu_heartbeat = now
            if now >= self.next_hu_heartbeat:
                self._enqueue(
                    1,
                    0x165,
                    bytes((0xC0, self.hu_source, 0x20, 0x00)),
                    "HU HEARTBEAT",
                    now,
                )
                while self.next_hu_heartbeat <= now:
                    self.next_hu_heartbeat += self.HU_HEARTBEAT_PERIOD

        if self.playback in ("playing", "paused"):
            if self.next_playback is None:
                self.next_playback = now
            if now >= self.next_playback:
                payload = (
                    bytes((0x00, 0x0B, 0x00))
                    if self.playback == "playing"
                    else bytes((0x00, 0x02, 0x00))
                )
                self._enqueue(
                    1,
                    0x325,
                    payload,
                    f"PLAYBACK {self.playback.upper()}",
                    now,
                )
                while self.next_playback <= now:
                    self.next_playback += self.PLAYBACK_PERIOD

        if self.playback == "playing":
            if self.next_position is None:
                self.next_position = now
            if now >= self.next_position:
                self._enqueue(
                    1,
                    0x3A5,
                    encode_track_position(
                        self.track_index,
                        self.duration,
                        self._current_position(now),
                    ),
                    "TRACK POSITION TICK",
                    now,
                )
                while self.next_position <= now:
                    self.next_position += self.POSITION_PERIOD

    def tick(self, max_frames=32):
        now = time.monotonic()
        self._schedule_periodic(now)

        count = 0
        while self.queue and count < max_frames:
            due, seq, channel, can_id, payload, origin = self.queue[0]
            if due > time.monotonic():
                break

            heapq.heappop(self.queue)
            self._send(channel, can_id, payload, origin)
            count += 1

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
    def __init__(self, path, scheduler, logger):
        self.path = Path(path)
        self.scheduler = scheduler
        self.log = logger
        self.clients = {}

        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.setblocking(False)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o660)
        self.server.listen(8)

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
        except FileNotFoundError:
            pass
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
        return (
            json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

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
            notice = self._json_line({
                "event": "dropped",
                "mono": time.monotonic(),
                "count": client.dropped,
            })
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
                "api_version": 1,
            })

    def _handle_request(self, client, request):
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")

        cmd = str(request.get("cmd", "")).strip().lower()
        if not cmd:
            raise ValueError("missing cmd")

        if cmd == "status":
            return {"ok": True, "state": self.scheduler.snapshot()}

        if cmd == "subscribe":
            client.sub_can = bool(request.get("can", False))
            client.sub_semantic = bool(request.get("semantic", False))

            channels = request.get("channels", [1, 2])
            directions = request.get("directions", ["rx", "tx"])

            client.channels = {int(v) for v in channels if int(v) in (1, 2)}
            client.directions = {
                str(v).lower()
                for v in directions
                if str(v).lower() in ("rx", "tx")
            }

            if not client.channels:
                raise ValueError("subscription has no valid channels")
            if client.sub_can and not client.directions:
                raise ValueError("CAN subscription has no valid directions")

            return {
                "ok": True,
                "subscription": {
                    "can": client.sub_can,
                    "semantic": client.sub_semantic,
                    "channels": sorted(client.channels),
                    "directions": sorted(client.directions),
                },
            }

        if cmd == "display.enable":
            self.scheduler.display_enable(request.get("enabled", True))
            return {"ok": True}

        if cmd == "display.button":
            self.scheduler.display_button(request.get("button", ""))
            return {"ok": True}

        if cmd == "hu.enable":
            self.scheduler.hu_enable(request.get("enabled", True))
            return {"ok": True}

        if cmd == "hu.source_raw":
            self.scheduler.hu_source_raw(request.get("value"))
            return {"ok": True, "source": f"{self.scheduler.hu_source:02X}"}

        if cmd == "track.set":
            self.scheduler.set_track(
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
            self.scheduler.set_position(
                request["position"],
                allow_overflow=request.get("allow_overflow"),
            )
            return {"ok": True}

        if cmd == "playback.set":
            self.scheduler.set_playback(request.get("state", ""))
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
                read_fds,
                write_fds,
                [],
                max(0.0, float(timeout)),
            )
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


def compute_wait(*deadlines, maximum=0.050):
    now = time.monotonic()
    waits = [maximum]
    for deadline in deadlines:
        if deadline is not None:
            waits.append(max(0.0, deadline - now))
    return min(waits)


def main():
    if not wait_for_carberry():
        clear_status_leds_best_effort()
        return 0

    if stop_requested:
        clear_status_leds_best_effort()
        return 0

    log = BridgeLog()
    cb = None
    status = None
    api = None

    try:
        events = EventHub(log)
        cb = CarBerry(log, can_rx_callback=events.can_rx)
        status = StatusLEDs(cb)

        status.initializing()

        if not require(cb, status, "CAN MODE USER"):
            return 1
        if not require(cb, status, "CAN USER ALIGN RIGHT"):
            return 1

        for channel, (name, bitrate) in CHANNELS.items():
            log.write("INFO", f"Opening {channel}={name} bitrate={bitrate}")

            if not require(cb, status, f"CAN USER OPEN {channel} {bitrate}"):
                return 1
            if not require(cb, status, f"CAN USER MASK {channel} 0000"):
                return 1
            if not require(cb, status, f"CAN USER FILTER {channel} 0 0000"):
                return 1

        require(cb, status, "CAN MODE")
        require(cb, status, "CAN USER ALIGN")
        status.can_ready()

        scheduler = TxScheduler(cb, events, log)
        api = ApiServer(API_SOCKET, scheduler, log)
        events.attach_api(api)

        log.write(
            "INFO",
            "BRIDGE READY: passive by default; CAN TX only after API state/commands",
            force_sync=True,
        )

        print("===== PEUGEOT / CARBERRY BRIDGE dev03 =====", flush=True)
        print("CH1: Peugeot 125K", flush=True)
        print("CH2: Harmony 250K", flush=True)
        print(f"API: {API_SOCKET}", flush=True)
        print("Raw capture: external subscriber only", flush=True)

        while not stop_requested:
            # Drain vehicle traffic first, then execute due TX, then service API.
            cb.drain_async()
            scheduler.tick()
            status.tick()

            wait = compute_wait(
                scheduler.next_deadline(),
                status.next_deadline(),
                maximum=0.050,
            )
            api.poll(wait)

    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        log.write("FATAL", repr(exc), force_sync=True)

        if status is not None:
            try:
                status.error()
            except Exception:
                pass

        return 1

    finally:
        if api is not None:
            api.close()

        if cb is not None:
            cb.close()

        clear_status_leds_best_effort(log)
        log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
