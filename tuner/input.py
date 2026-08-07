"""Four verbs. Every input device reduces to these.

The binding constraint: the whole appliance must be operable from a presentation clicker,
which is four buttons. A keyboard is welcome, it just must never be *required* — if any flow
can only be completed by typing, that flow is a bug.

So the entire on-device vocabulary is:

    UP      previous channel / move cursor up
    DOWN    next channel / move cursor down
    SELECT  open the menu / activate the highlighted item
    BACK    dismiss / go up one level

CEC remotes and keyboards offer more than four keys, and where they do we accept the extras
(digits for direct channel entry, a dedicated power key). Nothing depends on them.

Clickers enumerate as USB HID keyboards. They don't agree on which keys they send, so the
mapping below is deliberately generous — Logitech, Kensington and the no-name ones all land
somewhere in it, which means no per-device setup.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator


class Verb(Enum):
    UP = "up"
    DOWN = "down"
    SELECT = "select"
    BACK = "back"
    # Optional. Present on CEC remotes and keyboards, absent on clickers. Never required.
    DIGIT = "digit"
    POWER = "power"


@dataclass(frozen=True)
class Event:
    verb: Verb
    digit: int | None = None
    source: str = "unknown"


# Linux input-event key codes (linux/input-event-codes.h), used by the evdev driver.
# Generous on purpose: a clicker's "next" might be PAGEDOWN, RIGHT, DOWN or SPACE depending
# on who made it, and asking the user to identify their clicker is a setup step we refuse.
EVDEV_MAP: dict[int, Verb] = {
    109: Verb.DOWN,    # PAGEDOWN  - clicker "next"
    108: Verb.DOWN,    # DOWN
    106: Verb.DOWN,    # RIGHT
    57:  Verb.DOWN,    # SPACE     - clicker "next" on some models
    104: Verb.UP,      # PAGEUP    - clicker "previous"
    103: Verb.UP,      # UP
    105: Verb.UP,      # LEFT
    28:  Verb.SELECT,  # ENTER
    96:  Verb.SELECT,  # KPENTER
    63:  Verb.SELECT,  # F5        - clicker "start presentation"
    48:  Verb.SELECT,  # B         - clicker "blank screen"
    52:  Verb.SELECT,  # DOT       - clicker "blank screen" on some models
    1:   Verb.BACK,    # ESC       - clicker "end presentation"
    14:  Verb.BACK,    # BACKSPACE
    116: Verb.POWER,   # POWER
}

EVDEV_DIGITS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9, 11: 0}

# CEC user-control codes (HDMI CEC spec, UI Command). The TV's own remote lands here.
CEC_MAP: dict[int, Verb] = {
    0x01: Verb.UP,
    0x02: Verb.DOWN,
    0x00: Verb.SELECT,   # SELECT/OK
    0x0D: Verb.BACK,     # EXIT
    0x30: Verb.UP,       # CHANNEL UP
    0x31: Verb.DOWN,     # CHANNEL DOWN
    0x40: Verb.POWER,
    0x6B: Verb.POWER,    # POWER OFF FUNCTION
}

CEC_DIGITS = {0x20 + n: n for n in range(10)}


class Driver:
    """Anything that can produce Events."""

    name = "driver"

    def events(self) -> Iterator[Event]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class EvdevDriver(Driver):
    """Reads a Linux input device directly — clickers, USB IR receivers, keyboards.

    Reading evdev rather than the terminal means this works with no X server, no console
    focus, and while mpv owns the framebuffer.
    """

    name = "evdev"

    def __init__(self, device_path: str):
        self.device_path = device_path
        self._file = None

    def events(self) -> Iterator[Event]:
        import struct

        # struct input_event: two longs (time), then type, code, value.
        fmt = "llHHi"
        size = struct.calcsize(fmt)

        self._file = open(self.device_path, "rb", buffering=0)
        while True:
            data = self._file.read(size)
            if not data or len(data) < size:
                break
            _, _, etype, code, value = struct.unpack(fmt, data)
            if etype != 0x01 or value != 1:  # EV_KEY, key-down only
                continue
            if code in EVDEV_DIGITS:
                yield Event(Verb.DIGIT, digit=EVDEV_DIGITS[code], source=self.name)
            elif code in EVDEV_MAP:
                yield Event(EVDEV_MAP[code], source=self.name)

    def close(self) -> None:
        if self._file:
            self._file.close()


def discover_evdev_devices() -> list[tuple[str, str]]:
    """Find plausible input devices without asking the user to identify their clicker.

    Anything advertising keyboard-ish keys qualifies, which is exactly what a presentation
    clicker looks like to the kernel.
    """
    devices: list[tuple[str, str]] = []
    try:
        with open("/proc/bus/input/devices") as handle:
            blocks = handle.read().split("\n\n")
    except OSError:
        return devices

    for block in blocks:
        name = ""
        handlers = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
        if "kbd" not in handlers:
            continue
        for token in handlers.split():
            if token.startswith("event"):
                devices.append((f"/dev/input/{token}", name))
                break
    return devices


class StdinDriver(Driver):
    """Terminal driver for development on a machine with no clicker attached."""

    name = "stdin"

    KEYS = {
        "\x1b[B": Verb.DOWN, "j": Verb.DOWN, " ": Verb.DOWN,
        "\x1b[A": Verb.UP, "k": Verb.UP,
        "\r": Verb.SELECT, "\n": Verb.SELECT,
        "\x1b": Verb.BACK, "q": Verb.BACK,
    }

    def events(self) -> Iterator[Event]:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                char = sys.stdin.read(1)
                if not char:
                    break
                if char == "\x1b":
                    # Could be a bare Esc (BACK) or the head of an arrow sequence.
                    nxt = sys.stdin.read(1)
                    if nxt == "[":
                        char += nxt + sys.stdin.read(1)
                    else:
                        yield Event(Verb.BACK, source=self.name)
                        continue
                if char.isdigit():
                    yield Event(Verb.DIGIT, digit=int(char), source=self.name)
                elif char in self.KEYS:
                    yield Event(self.KEYS[char], source=self.name)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class CecDriver(Driver):
    """HDMI-CEC, so the TV's own remote drives the box.

    Three silent failure modes, all worth knowing because none of them produce an error:
      - CEC only works on HDMI0 (the port nearest USB-C) on both Pi 4 and Pi 5.
      - A trailing `D` on a `video=HDMI-A-1:` kernel cmdline forces DVI mode and suppresses
        CEC initialisation entirely.
      - An AVR or soundbar in the HDMI chain frequently breaks CEC relay, with no fix.

    So a boot-time self-test and an on-screen diagnostic card matter as much as this driver.
    """

    name = "cec"

    def __init__(self, device: str = "/dev/cec0"):
        self.device = device
        self._proc = None

    def available(self) -> bool:
        import shutil
        from pathlib import Path
        return Path(self.device).exists() and shutil.which("cec-ctl") is not None

    def events(self) -> Iterator[Event]:
        import re
        import subprocess

        self._proc = subprocess.Popen(
            ["cec-ctl", "-d", self.device, "--monitor"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        pattern = re.compile(r"ui-cmd:\s*(?:0x)?([0-9a-fA-F]+)")
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            match = pattern.search(line)
            if not match:
                continue
            code = int(match.group(1), 16)
            if code in CEC_DIGITS:
                yield Event(Verb.DIGIT, digit=CEC_DIGITS[code], source=self.name)
            elif code in CEC_MAP:
                yield Event(CEC_MAP[code], source=self.name)

    def close(self) -> None:
        if self._proc:
            self._proc.terminate()


def multiplex(drivers: list[Driver], on_event: Callable[[Event], None]) -> None:
    """Run every available driver at once, feeding one handler.

    All of them, always — a CEC remote and a clicker should both work without the user
    choosing, and if CEC turns out to be broken on their TV the clicker just keeps working.
    """
    import threading

    def pump(driver: Driver) -> None:
        try:
            for event in driver.events():
                on_event(event)
        except Exception:  # noqa: BLE001 - one dead driver must not take the others down
            pass

    threads = [threading.Thread(target=pump, args=(d,), daemon=True) for d in drivers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
