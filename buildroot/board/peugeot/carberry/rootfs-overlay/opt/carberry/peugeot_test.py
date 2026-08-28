#!/usr/bin/env python3

import os
import select
import sys
import termios
import time
import tty

from peugeot_api_client import PeugeotApiClient, PeugeotApiError


ICON_SCAN_HOLD = 2.5


def req(api, obj):
    return api.request(obj)


def set_track(
    api,
    artist,
    title,
    duration=None,
    position=0,
    text_mode="translit",
    allow_overflow=False,
    playback="playing",
):
    req(api, {
        "cmd": "track.set",
        "artist": artist,
        "title": title,
        "duration": duration,
        "position": position,
        "text_mode": text_mode,
        "allow_overflow": allow_overflow,
        "playback": playback,
    })


def demo_metadata_time(api):
    """
    Key 5 diagnostic sequence.

    Phase 1: five seconds of Cyrillic encoded as CP1251. This intentionally
    tests the display rather than the production transliterator.

    Phase 2: duration=5 s, current position intentionally allowed to run to
    about 10 s. Bridge clamp is disabled for this phase so the vehicle display
    itself is being tested.
    """
    req(api, {"cmd": "hu.enable", "enabled": True})

    set_track(
        api,
        artist="КИРИЛЛИЦА",
        title="УТЕКАЙ",
        duration=5,
        position=0,
        text_mode="cp1251",
        allow_overflow=False,
        playback="playing",
    )
    time.sleep(5.5)

    set_track(
        api,
        artist="TOTAL 00:05",
        title="ASCII OVERFLOW",
        duration=5,
        position=0,
        text_mode="ascii",
        allow_overflow=True,
        playback="playing",
    )
    time.sleep(10.5)

    req(api, {"cmd": "playback.set", "state": "paused"})


def demo_icon_scan(api):
    """Key 6: scan 0x165 second byte C0..CF, self-labelled on display."""
    req(api, {"cmd": "hu.enable", "enabled": True})

    for value in range(0xC0, 0xD0):
        req(api, {"cmd": "hu.source_raw", "value": value})
        set_track(
            api,
            artist=f"SOURCE {value:02X}",
            title=f"165 C0 {value:02X} 20 00",
            duration=None,
            position=0,
            text_mode="ascii",
            allow_overflow=False,
            playback="playing",
        )
        time.sleep(ICON_SCAN_HOLD)

    req(api, {"cmd": "playback.set", "state": "paused"})


def read_key(fd):
    ch = os.read(fd, 1)
    if ch != b"\x1b":
        return ch.decode("utf-8", errors="ignore")

    seq = bytearray()
    deadline = time.monotonic() + 0.060
    while time.monotonic() < deadline and len(seq) < 2:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            break
        seq.extend(os.read(fd, 1))

    mapping = {
        b"[A": "UP",
        b"[B": "DOWN",
        b"[C": "RIGHT",
        b"[D": "LEFT",
    }
    return mapping.get(bytes(seq), "ESC")


def help_text():
    print(
        "Peugeot test client\n"
        "  arrows  DISPLAY UP/DOWN/LEFT/RIGHT\n"
        "  Enter/Space  DISPLAY OK (manual only)\n"
        "  Esc     DISPLAY EXIT\n"
        "  M       DISPLAY MENU\n"
        "  D       DISPLAY DARK\n"
        "  [ / ]   HU source byte -/+\n"
        "  5       metadata/Cyrillic/time-overflow demo\n"
        "  6       0x165 C0..CF icon scan\n"
        "  H       help\n"
        "  Q       quit\n",
        flush=True,
    )


def main():
    if not sys.stdin.isatty():
        print("ERROR: peugeot-test needs an interactive terminal", file=sys.stderr)
        return 2

    try:
        api = PeugeotApiClient()
    except (OSError, PeugeotApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    source = 0xC4

    try:
        # While the interactive tester is alive, reproduce the old AM2
        # periodic DISPLAY ZERO cadence. No automatic scenario ever sends OK.
        req(api, {"cmd": "display.enable", "enabled": True})
        help_text()

        tty.setcbreak(fd)

        while True:
            key = read_key(fd)
            upper = key.upper() if isinstance(key, str) else key

            if upper in ("Q", "\x03"):
                break
            if upper == "H":
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                help_text()
                tty.setcbreak(fd)
                continue

            if upper in ("UP", "DOWN", "LEFT", "RIGHT"):
                req(api, {"cmd": "display.button", "button": upper})
                continue

            if key in ("\r", "\n", " "):
                req(api, {"cmd": "display.button", "button": "OK"})
                continue

            if upper == "ESC":
                req(api, {"cmd": "display.button", "button": "EXIT"})
                continue
            if upper == "M":
                req(api, {"cmd": "display.button", "button": "MENU"})
                continue
            if upper == "D":
                req(api, {"cmd": "display.button", "button": "DARK"})
                continue

            if key == "[":
                source = max(0, source - 1)
                req(api, {"cmd": "hu.source_raw", "value": source})
                continue
            if key == "]":
                source = min(0xFF, source + 1)
                req(api, {"cmd": "hu.source_raw", "value": source})
                continue

            if key == "5":
                demo_metadata_time(api)
                continue
            if key == "6":
                demo_icon_scan(api)
                source = 0xCF
                continue

    except KeyboardInterrupt:
        pass

    except (OSError, PeugeotApiError, ValueError) as exc:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        try:
            req(api, {"cmd": "display.enable", "enabled": False})
            req(api, {"cmd": "playback.set", "state": "stopped"})
            req(api, {"cmd": "hu.enable", "enabled": False})
        except Exception:
            pass
        api.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
