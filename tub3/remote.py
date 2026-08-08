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


def all_event_nodes() -> list[str]:
    """Every event node, not just the keyboard-ish ones.

    `discover_evdev_devices` filters to devices advertising `kbd`, which is right for the
    tuner — it is looking for something that can drive the box. It is wrong for diagnosis.
    A presentation clicker routinely enumerates as *two* devices, a keyboard and a mouse,
    and a button that arrives on the mouse node is invisible to the filtered list. Watching
    only the keyboard node and concluding a button "does nothing" is the exact dead end
    this module exists to prevent.
    """
    return sorted((str(p) for p in Path("/dev/input").glob("event*")),
                  key=lambda p: int(p.rsplit("event", 1)[1]))


EV_TYPES = {0x00: "SYN", 0x01: "KEY", 0x02: "REL", 0x03: "ABS", 0x04: "MSC", 0x11: "LED",
            0x14: "REP"}

# Modifiers arrive alongside the key they modify — a clicker's long-press is often
# Shift+F5 or Alt+Tab. They are correctly ignored by the tuner, but reporting them as
# "UNMAPPED" in a diagnostic invites someone to go and map them, which would fire a
# spurious verb on every long-press. Name them instead.
MODIFIERS = {42: "LEFTSHIFT", 54: "RIGHTSHIFT", 29: "LEFTCTRL", 97: "RIGHTCTRL",
             56: "LEFTALT", 100: "RIGHTALT", 125: "LEFTMETA", 126: "RIGHTMETA"}


def watch(devices: list[str], seconds: float | None = None, raw: bool = False) -> int:
    """Print every key press across every device, and whether the box would understand it.

    Watches all of them at once. `seconds` bounds the run so this can be driven from a
    remote shell, where there is nobody at the keyboard to press Ctrl+C.

    `raw` drops the EV_KEY/key-down filter and prints every event of every type. Silence in
    the filtered view is ambiguous — a button that sends only a scancode (EV_MSC), or that
    drives a laser diode in hardware and sends nothing at all, look identical to a button
    that is not being pressed. Raw distinguishes them, which turns "maybe it's broken" into
    a fact.
    """
    import select
    import time

    handles: dict[int, tuple[str, object]] = {}
    denied: list[str] = []
    for path in devices:
        try:
            handle = open(path, "rb", buffering=0)
        except OSError:
            denied.append(path)
            continue
        handles[handle.fileno()] = (path, handle)

    if not handles:
        print(f"\n  Cannot read any input device ({len(denied)} tried).")
        print("  Input devices need root: prefix the command with sudo.\n")
        return 1

    names = {path: name for path, name in discover_evdev_devices()}
    limit = f" for {seconds:g}s" if seconds else ""
    print(f"\n  Watching {len(handles)} device(s){limit}. Press every button on the remote.\n")
    for _, (path, _h) in sorted(handles.items()):
        print(f"    {path:<20} {names.get(path, '')}")
    if denied:
        print(f"\n    ({len(denied)} not readable — run with sudo to include them)")
    print(f"\n    {'device':<16} {'keycode':<9} {'maps to':<12} {'note'}")
    print("    " + "-" * 66)

    seen: set[int] = set()
    deadline = time.monotonic() + seconds if seconds else None
    try:
        while True:
            timeout = None
            if deadline is not None:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
            ready, _, _ = select.select(list(handles), [], [], timeout)
            for fd in ready:
                path, handle = handles[fd]
                data = handle.read(EVENT_SIZE)
                if not data or len(data) < EVENT_SIZE:
                    continue
                _, _, etype, code, value = struct.unpack(EVENT_FORMAT, data)

                if raw:
                    if etype == 0x00:                  # SYN, one after every event group
                        continue
                    short = path.replace("/dev/input/", "")
                    kind = EV_TYPES.get(etype, f"0x{etype:02x}")
                    edge = {0: "up", 1: "down", 2: "repeat"}.get(value, str(value)) \
                        if etype == EV_KEY else str(value)
                    known = ""
                    if etype == EV_KEY and value == 1:
                        seen.add(code)
                        if code in EVDEV_MAP:
                            known = f"  -> {EVDEV_MAP[code].value}"
                        elif code in EVDEV_DIGITS:
                            known = f"  -> digit {EVDEV_DIGITS[code]}"
                        elif code in MODIFIERS:
                            known = f"  ({MODIFIERS[code]}, modifier — ignored, correctly)"
                        else:
                            known = "  -> UNMAPPED"
                    print(f"    {short:<16} {kind:<5} code={code:<6} {edge}{known}",
                          flush=True)
                    continue

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
                short = path.replace("/dev/input/", "")
                print(f"    {short:<16} {code:<9} {verb:<12} {note}{flag}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for _path, handle in handles.values():
            handle.close()

    missing = [v for v in (Verb.UP, Verb.DOWN, Verb.SELECT, Verb.BACK)
               if not any(EVDEV_MAP.get(c) is v for c in seen)]
    print()
    if not seen:
        print("  Nothing arrived. The receiver may be unplugged, or the remote asleep —")
        print("  most clickers power down and need a button held to wake.\n")
        return 1
    if missing:
        print("  Not seen: " + ", ".join(v.value for v in missing))
        print("  All four are needed to drive the box from this remote alone.\n")
        return 1
    print("  All four verbs present — this remote can drive the box on its own.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.remote", description=__doc__)
    ap.add_argument("action", nargs="?", default="list", choices=["list", "watch"])
    ap.add_argument("--device", help="/dev/input/eventN (default: watch all of them)")
    ap.add_argument("--seconds", type=float,
                    help="stop after N seconds, for driving this from a remote shell")
    ap.add_argument("--raw", action="store_true",
                    help="print every event of every type, not just mapped key presses")
    args = ap.parse_args(argv)

    if args.action == "list":
        return list_devices()

    devices = [args.device] if args.device else all_event_nodes()
    if not devices:
        return list_devices()
    return watch(devices, seconds=args.seconds, raw=args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
