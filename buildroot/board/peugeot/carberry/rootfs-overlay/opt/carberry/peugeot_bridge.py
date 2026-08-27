#!/usr/bin/env python3

import fcntl
import os
import signal
import socket
import sys
import time
from pathlib import Path


HOST = "127.0.0.1"
PORT = 7070

# Permanent project wiring:
#   CH1 = Peugeot vehicle CAN
#   CH2 = MiniDSP Harmony CAN
CHANNELS = {
    "CH1": ("PEUGEOT", "125K"),
    "CH2": ("HARMONY", "250K"),
}

LOG_DIR = Path("/var/log/carberry/captures")
COUNTER_FILE = LOG_DIR / ".capture_counter"

stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


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

                line = raw.decode(
                    "ascii",
                    errors="backslashreplace"
                )

                if line == "OK":
                    return True

                if line.startswith("ERROR"):
                    return False

        except socket.timeout:
            pass

    return False


def clear_status_leds_best_effort(logger=None):
    """
    Graceful shutdown cleanup.

    Use a NEW TCP connection because the main bridge connection may
    already be leaving its receive loop after SIGTERM.

    carberry_d must still be alive while this function runs.
    """
    try:
        with socket.create_connection(
            (HOST, PORT),
            timeout=1.5
        ) as sock:
            sock.settimeout(0.20)

            green_ok = fresh_socket_command(
                sock,
                "GPLED LED1 CLEAR"
            )

            red_ok = fresh_socket_command(
                sock,
                "GPLED LED2 CLEAR"
            )

            if logger is not None:
                logger.write(
                    "INFO",
                    "STATUS LED shutdown clear: "
                    f"green={green_ok} red={red_ok}",
                    force_sync=True
                )

            return green_ok and red_ok

    except Exception as exc:
        if logger is not None:
            logger.write(
                "WARN",
                f"STATUS LED shutdown clear failed: {exc!r}",
                force_sync=True
            )

        return False


def wait_for_carberry():
    """
    Wait until TCP is accepting connections AND the CarBerry/PIC
    command processor actually responds.

    Opening TCP :7070 alone is not sufficient.

    No capture number and no capture file are created while waiting.
    """
    print(
        "Waiting for CarBerry command interface...",
        flush=True
    )

    while not stop_requested:
        sock = None

        try:
            sock = socket.create_connection(
                (HOST, PORT),
                timeout=2.0
            )

            sock.settimeout(0.20)

            # carberry_d waits up to ~5 s for a UART reply from the PIC.
            # Our client must outlive that timeout; otherwise a missing/unready
            # PIC leaves overlapping abandoned TCP requests in the daemon.
            if fresh_socket_command(
                sock,
                "CAN MODE",
                timeout=6.0
            ):
                print(
                    "CarBerry command interface ready.",
                    flush=True
                )
                return True

        except (OSError, ConnectionError):
            pass

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        # Sleep in short pieces so SIGTERM remains responsive.
        for _ in range(10):
            if stop_requested:
                return False
            time.sleep(0.1)

    return False


def next_capture_number():
    """
    Persistent capture counter, independent of RTC/system date.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with COUNTER_FILE.open("a+", encoding="ascii") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

        f.seek(0)
        text = f.read().strip()

        try:
            number = int(text)
        except (ValueError, TypeError):
            number = 0

        number += 1

        f.seek(0)
        f.truncate()
        f.write(f"{number}\n")
        f.flush()
        os.fsync(f.fileno())

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return number


class Logger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self.capture = next_capture_number()

        try:
            self.boot_id = Path(
                "/proc/sys/kernel/random/boot_id"
            ).read_text().strip()
        except Exception:
            self.boot_id = "unknown"

        boot_short = self.boot_id[:8]

        self.path = LOG_DIR / (
            f"capture-{self.capture:06d}-{boot_short}.log"
        )

        self.f = self.path.open(
            "w",
            encoding="utf-8",
            buffering=1
        )

        self.last_fsync = time.monotonic()

        self.f.write(
            "================================================================\n"
            "Peugeot / CarBerry dual CAN capture\n"
            f"Capture: {self.capture:06d}\n"
            f"Boot ID: {self.boot_id}\n"
            "CH1: PEUGEOT | 125K\n"
            "CH2: HARMONY | 250K\n"
            "Time base: CLOCK_MONOTONIC\n"
            "Wall clock: NOT USED\n"
            "Mode: PASSIVE LOGGER - NO CAN TX\n"
            "================================================================\n"
        )

        self.f.flush()
        os.fsync(self.f.fileno())

        self.write(
            "INFO",
            "SESSION START",
            force_sync=True
        )

    def write(self, direction, text, force_sync=False):
        mono = time.monotonic()

        self.f.write(
            f"{mono:14.6f}  "
            f"{direction:<5}  "
            f"{text}\n"
        )

        self.f.flush()

        now = time.monotonic()

        if force_sync or now - self.last_fsync >= 1.0:
            os.fsync(self.f.fileno())
            self.last_fsync = now

    def close(self):
        try:
            self.write(
                "INFO",
                "SESSION END",
                force_sync=True
            )

            self.f.close()

        except Exception:
            pass


class CarBerry:
    def __init__(self, logger):
        self.log = logger
        self.buf = bytearray()

        self.sock = socket.create_connection(
            (HOST, PORT),
            timeout=3.0
        )

        # Gives sufficient resolution for 125/150 ms LED patterns.
        self.sock.settimeout(0.05)

        self.log.write(
            "INFO",
            f"TCP connected to {HOST}:{PORT}",
            force_sync=True
        )

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _read_line(self, deadline=None):
        while not stop_requested:
            pos = self.buf.find(b"\r\n")

            if pos >= 0:
                raw = bytes(self.buf[:pos])
                del self.buf[:pos + 2]

                line = raw.decode(
                    "ascii",
                    errors="backslashreplace"
                )

                # Log command replies and asynchronous CAN traffic.
                self.log.write("RX", line)

                return line

            if (
                deadline is not None
                and time.monotonic() >= deadline
            ):
                return None

            try:
                data = self.sock.recv(4096)

                if not data:
                    raise ConnectionError(
                        "CarBerry daemon closed connection"
                    )

                self.buf.extend(data)

            except socket.timeout:
                pass

        return None

    def command(
        self,
        command,
        timeout=3.0,
        quiet=False
    ):
        if not quiet:
            print(f">>> {command}", flush=True)

        self.log.write("CMD", command)

        # Confirmed command terminator: CR.
        self.sock.sendall(
            command.encode("ascii") + b"\r"
        )

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
                    force_sync=True
                )

                if not quiet:
                    print(
                        f"TIMEOUT: {command}",
                        file=sys.stderr,
                        flush=True
                    )

                return False, payload

            if not quiet:
                print(f"<<< {line}", flush=True)

            if line == "OK":
                return True, payload

            if line.startswith("ERROR"):
                return False, payload

            payload.append(line)

        return False, payload

    def listen(self, status):
        self.log.write(
            "INFO",
            "PASSIVE CAN LISTENER STARTED",
            force_sync=True
        )

        print()
        print("===== PEUGEOT / CARBERRY BRIDGE =====")
        print(f"Capture : {self.log.capture:06d}")
        print(f"Log     : {self.log.path}")
        print("CH1     : Peugeot 125K")
        print("CH2     : Harmony 250K")
        print("CAN TX  : disabled")
        print()

        while not stop_requested:
            line = self._read_line(
                time.monotonic() + 0.05
            )

            if line is not None:
                print(
                    f"{time.monotonic():14.6f}  {line}",
                    flush=True
                )

            status.tick()


class StatusLEDs:
    """
    CarBerry HW 1.00 / FW 1.19:

        GPLED LED1 = GREEN
        GPLED LED2 = RED

    The early boot RED solid is produced separately by direct UART.

    Once this bridge has confirmed the CarBerry API, all LED commands
    are sent ONLY through carberry_d TCP.
    """

    GREEN = "LED1"
    RED = "LED2"

    # API/PIC ready, CAN initialization in progress.
    # One short red pulse every second.
    PATTERN_INITIALIZING = (
        (True,  0.250),
        (False, 0.750),
    )

    # CAN ready, synchronization not yet confirmed.
    PATTERN_UNSYNCED = (
        (True,  0.150),
        (False, 1.850),
    )

    # Fully synchronized.
    PATTERN_SYNCED = (
        (True,  0.150),
        (False, 0.150),
        (True,  0.150),
        (False, 1.550),
    )

    # Runtime error hook.
    PATTERN_ERROR = (
        (True,  0.125),
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

        current = (
            self.green
            if led == self.GREEN
            else self.red
        )

        if current is on:
            return True

        action = "SET" if on else "CLEAR"

        try:
            ok, _ = self.cb.command(
                f"GPLED {led} {action}",
                timeout=1.0,
                quiet=True
            )

        except Exception as exc:
            self.cb.log.write(
                "WARN",
                f"STATUS LED EXCEPTION: "
                f"{led} {action}: {exc!r}",
                force_sync=True
            )

            self.enabled = False
            return False

        if not ok:
            self.cb.log.write(
                "WARN",
                f"STATUS LED COMMAND FAILED: "
                f"{led} {action}",
                force_sync=True
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
            self.next_transition = (
                time.monotonic() + duration
            )

    def initializing(self):
        self.mode = "initializing"
        self.volume_synced = False
        self._stop_pattern()

        self._set(self.GREEN, False)

        # Early boot left RED solid.
        # This TCP-driven pattern takes ownership from here.
        self._start_pattern(
            self.RED,
            self.PATTERN_INITIALIZING
        )

        self.cb.log.write(
            "INFO",
            "STATUS: RED heartbeat - "
            "CarBerry API ready, CAN initializing"
        )

    def can_ready(self):
        self.mode = "can_ready"
        self.volume_synced = False
        self._stop_pattern()

        self._set(self.RED, False)
        self._set(self.GREEN, False)

        self._start_pattern(
            self.GREEN,
            self.PATTERN_UNSYNCED
        )

        self.cb.log.write(
            "INFO",
            "STATUS: GREEN single heartbeat - "
            "CAN ready, synchronization not confirmed",
            force_sync=True
        )

    def set_volume_synced(self, synced=True):
        """
        Future synchronization hook.

        It is intentionally NOT called yet.
        """
        self.volume_synced = bool(synced)

        if not self.volume_synced:
            self.can_ready()
            return

        self.mode = "synced"
        self._stop_pattern()

        self._set(self.RED, False)
        self._set(self.GREEN, False)

        self._start_pattern(
            self.GREEN,
            self.PATTERN_SYNCED
        )

        self.cb.log.write(
            "INFO",
            "STATUS: GREEN double heartbeat - synchronized",
            force_sync=True
        )

    def error(self):
        self.mode = "error"
        self.volume_synced = False
        self._stop_pattern()

        self._set(self.GREEN, False)
        self._set(self.RED, False)

        self._start_pattern(
            self.RED,
            self.PATTERN_ERROR
        )

    def tick(self):
        if (
            not self.enabled
            or self.pattern_led is None
            or not self.pattern
        ):
            return

        now = time.monotonic()

        if now < self.next_transition:
            return

        self.pattern_index = (
            self.pattern_index + 1
        ) % len(self.pattern)

        state, duration = self.pattern[self.pattern_index]

        if self._set(self.pattern_led, state):
            self.next_transition = (
                time.monotonic() + duration
            )


def require(cb, status, command):
    ok, _ = cb.command(command)

    # Let startup heartbeat advance between initialization commands.
    status.tick()

    if not ok:
        cb.log.write(
            "ERR",
            f"COMMAND FAILED: {command}",
            force_sync=True
        )

        print(
            f"ERROR: command failed: {command}",
            file=sys.stderr,
            flush=True
        )

    return ok


def main():
    # No capture number/file until PIC command interface is genuinely ready.
    if not wait_for_carberry():
        # Best effort: graceful shutdown may happen while still waiting.
        clear_status_leds_best_effort()
        return 0

    if stop_requested:
        clear_status_leds_best_effort()
        return 0

    log = Logger()
    cb = None
    status = None

    print(
        f"LOG FILE: {log.path}",
        flush=True
    )

    try:
        cb = CarBerry(log)
        status = StatusLEDs(cb)

        # From this exact point onward LED control is TCP-only.
        status.initializing()

        if not require(cb, status, "CAN MODE USER"):
            return 1

        if not require(
            cb,
            status,
            "CAN USER ALIGN RIGHT"
        ):
            return 1

        for channel, (name, bitrate) in CHANNELS.items():
            log.write(
                "INFO",
                f"Opening {channel}={name} bitrate={bitrate}"
            )

            if not require(
                cb,
                status,
                f"CAN USER OPEN {channel} {bitrate}"
            ):
                return 1

            if not require(
                cb,
                status,
                f"CAN USER MASK {channel} 0000"
            ):
                return 1

            if not require(
                cb,
                status,
                f"CAN USER FILTER {channel} 0 0000"
            ):
                return 1

        # Record resulting configuration.
        require(cb, status, "CAN MODE")
        require(cb, status, "CAN USER ALIGN")

        status.can_ready()

        cb.listen(status)

    except Exception as exc:
        print(
            f"FATAL: {exc}",
            file=sys.stderr,
            flush=True
        )

        log.write(
            "FATAL",
            repr(exc),
            force_sync=True
        )

        if status is not None:
            try:
                status.error()
            except Exception:
                pass

        return 1

    finally:
        # Do not use the main connection for shutdown LED cleanup.
        if cb is not None:
            cb.close()

        clear_status_leds_best_effort(log)

        log.close()

        print(
            f"Saved: {log.path}",
            flush=True
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
