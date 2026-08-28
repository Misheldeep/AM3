#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from peugeot_api_client import PeugeotApiClient, PeugeotApiError


DEFAULT_DIR = Path("/var/log/carberry/captures")
stop_requested = False


def stop(signum, frame):
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def next_capture_number(directory):
    directory.mkdir(parents=True, exist_ok=True)
    counter = directory / ".capture_counter"

    with counter.open("a+", encoding="ascii") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        try:
            number = int(f.read().strip())
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


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return "unknown"


class CaptureWriter:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.capture = next_capture_number(self.directory)
        self.boot = boot_id()
        short = self.boot[:8]
        self.path = self.directory / f"capture-{self.capture:06d}-{short}.log"
        self.f = self.path.open("w", encoding="utf-8", buffering=1)
        self.last_fsync = time.monotonic()

        self.f.write(
            "================================================================\n"
            "Peugeot / CarBerry API CAN capture\n"
            f"Capture: {self.capture:06d}\n"
            f"Boot ID: {self.boot}\n"
            "CH1: PEUGEOT | 125K\n"
            "CH2: HARMONY | 250K\n"
            "Time base: CLOCK_MONOTONIC supplied by peugeot_bridge\n"
            "Wall clock: NOT USED\n"
            "Source: /run/peugeot-bridge.sock event subscription\n"
            "================================================================\n"
        )
        self.sync(True)

    def sync(self, force=False):
        now = time.monotonic()
        if force or now - self.last_fsync >= 1.0:
            self.f.flush()
            os.fsync(self.f.fileno())
            self.last_fsync = now

    def write_event(self, event):
        mono = float(event.get("mono", time.monotonic()))
        kind = event.get("event")

        if kind == "can":
            direction = str(event.get("direction", "?")).upper()
            channel = event.get("channel", "?")
            can_id = event.get("id", "?")
            data = event.get("data", "")
            suffix = ""
            if direction == "TX" and event.get("origin"):
                suffix = f"  origin={event['origin']}"
            self.f.write(
                f"{mono:14.6f}  {direction:<5}  CH{channel} "
                f"{can_id}-{data}{suffix}\n"
            )

        elif kind == "semantic":
            name = event.get("name", "?")
            fields = {
                k: v
                for k, v in event.items()
                if k not in ("event", "mono", "name")
            }
            self.f.write(
                f"{mono:14.6f}  EVENT  {name} "
                f"{json.dumps(fields, ensure_ascii=False, sort_keys=True)}\n"
            )

        elif kind == "dropped":
            self.f.write(
                f"{mono:14.6f}  WARN   SUBSCRIBER DROPPED "
                f"{event.get('count', '?')} EVENTS\n"
            )

        else:
            self.f.write(
                f"{mono:14.6f}  RAW    "
                f"{json.dumps(event, ensure_ascii=False, sort_keys=True)}\n"
            )

        self.sync(False)

    def close(self):
        try:
            self.f.write(f"{time.monotonic():14.6f}  INFO   SESSION END\n")
            self.sync(True)
            self.f.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Subscribe to peugeot_bridge CAN events and store a capture"
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_DIR),
        help="capture directory (future USB diagnostic mode will supply this)",
    )
    parser.add_argument(
        "--rx-only",
        action="store_true",
        help="store only received CAN frames (semantic events are still stored)",
    )
    args = parser.parse_args()

    writer = None
    api = None

    try:
        # File ownership belongs here, not in the bridge.
        writer = CaptureWriter(Path(args.output_dir))
        print(f"Capture: {writer.path}", flush=True)

        api = PeugeotApiClient()
        api.subscribe(
            can=True,
            semantic=True,
            channels=(1, 2),
            directions=("rx",) if args.rx_only else ("rx", "tx"),
        )

        while not stop_requested:
            try:
                event = api.recv(timeout=0.5)
            except TimeoutError:
                continue
            writer.write_event(event)

    except (OSError, PeugeotApiError, json.JSONDecodeError) as exc:
        if not stop_requested:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    finally:
        if api is not None:
            api.close()
        if writer is not None:
            writer.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
