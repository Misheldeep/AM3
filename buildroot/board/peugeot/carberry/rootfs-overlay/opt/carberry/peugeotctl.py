#!/usr/bin/env python3

import json
import sys

from peugeot_api_client import PeugeotApiClient, PeugeotApiError


def usage():
    print(
        "Usage:\n"
        "  peugeotctl status\n"
        "  peugeotctl display on|off\n"
        "  peugeotctl display BUTTON\n"
        "  peugeotctl hu on|off\n"
        "  peugeotctl hu source XX\n"
        "  peugeotctl playback playing|paused|stopped\n"
        "  peugeotctl json '{\"cmd\":...}'",
        file=sys.stderr,
    )


def main(argv):
    if len(argv) < 2:
        usage()
        return 2

    args = argv[1:]

    if args[0] == "status" and len(args) == 1:
        request = {"cmd": "status"}

    elif args[0] == "display" and len(args) == 2:
        value = args[1].upper()
        if value in ("ON", "OFF"):
            request = {"cmd": "display.enable", "enabled": value == "ON"}
        else:
            request = {"cmd": "display.button", "button": value}

    elif args[0] == "hu" and len(args) == 2 and args[1].lower() in ("on", "off"):
        request = {"cmd": "hu.enable", "enabled": args[1].lower() == "on"}

    elif args[0] == "hu" and len(args) == 3 and args[1].lower() == "source":
        request = {"cmd": "hu.source_raw", "value": args[2]}

    elif args[0] == "playback" and len(args) == 2:
        request = {"cmd": "playback.set", "state": args[1]}

    elif args[0] == "json" and len(args) == 2:
        request = json.loads(args[1])

    else:
        usage()
        return 2

    try:
        with PeugeotApiClient() as api:
            response = api.request(request)
    except (OSError, ValueError, json.JSONDecodeError, PeugeotApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
