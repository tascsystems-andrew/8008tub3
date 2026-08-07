"""Find out what your remote actually sends.

The historic Pi remote problem is not that remotes are hard, it is that they are opaque. A
clicker or an IR receiver enumerates as a keyboard, emits some keycodes nobody documented,
and if they are not the ones your software expects, nothing happens and nothing explains why.

So: list every input device, watch one, and print what arrives. If a button shows up here but
does nothing in the box, the mapping needs a line — and this tells you exactly which line.

Runs as root on a Pi, because /dev/input/event* is not world-readable.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from tuner.input import EVDEV_DIGITS, EVDEV_MAP, Verb, discover_evdev_devices

EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01


def list_devices() -> int:
    devices = discover_evdev_devices()
    if not devices:
        print("\n  No keyboard-like input devices found.")
        print("  A clicker or IR receiver should appear here once plugged in.")
        print("  If this is not a Linux box, there is nothing to list.\n")
        return 1

    print(f"\n  {len(devices)} input device(s):\n")
    for path, name in devices:
        readable = "" if Path(path).exists() and _readable(path) else "   (need sudo)"
        print(f"    {path:<22} {name}{readable}")
    print("\n  Watch one:  sudo python3 -m tub3.remote watch --device /dev/input/eventN\n")
    return 0


def _readable(path: str) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def watch(device: str) -> int:
    """Print every key press, and whether the box would understand it."""
    try:
        handle = open(device, "rb", buffering=0)
    except OSError as exc:
        print(f"\n  Cannot read {device}: {exc}")
        print("  Input devices need root: prefix the command with sudo.\n")
        return 1

    print(f"\n  Watching {device}. Press buttons on your remote. Ctrl+C to stop.\n")
    print(f"    {'keycode':<9} {'maps to':<10} {'note'}")
    print("    " + "-" * 52)

    seen: set[int] = set()
    try:
        while True:
            data = handle.read(EVENT_SIZE)
            if not data or len(data) < EVENT_SIZE:
                break
            _, _, etype, code, value = struct.unpack(EVENT_FORMAT, data)
            if etype != EV_KEY or value != 1:      # key-down only
                continue

            if code in EVDEV_DIGITS:
                verb, note = f"digit {EVDEV_DIGITS[code]}", "optional — never required"
            elif code in EVDEV_MAP:
                verb, note = EVDEV_MAP[code].value, "recognised"
            else:
                verb, note = "—", "UNMAPPED: add to EVDEV_MAP in tuner/input.py"
            flag = "" if code in seen else "  *"
            seen.add(code)
            print(f"    {code:<9} {verb:<10} {note}{flag}")
    except KeyboardInterrupt:
        pass
    finally:
        handle.close()

    missing = [v for v in (Verb.UP, Verb.DOWN, Verb.SELECT, Verb.BACK)
               if not any(EVDEV_MAP.get(c) is v for c in seen)]
    print()
    if missing:
        print("  Not seen yet: " + ", ".join(v.value for v in missing))
        print("  All four are needed to drive the box from this device alone.\n")
    else:
        print("  All four verbs present — this remote can drive the box on its own.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.remote", description=__doc__)
    ap.add_argument("action", nargs="?", default="list", choices=["list", "watch"])
    ap.add_argument("--device", help="/dev/input/eventN")
    args = ap.parse_args(argv)

    if args.action == "list":
        return list_devices()

    device = args.device
    if not device:
        devices = discover_evdev_devices()
        if not devices:
            return list_devices()
        device = devices[0][0]
        print(f"  (no --device given, using {device})")
    return watch(device)


if __name__ == "__main__":
    raise SystemExit(main())
