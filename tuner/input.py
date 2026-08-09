"""Four verbs. Every input device reduces to these.

The binding constraint: the whole appliance must be operable from a presentation clicker,
which is four buttons. A keyboard is welcome, it just must never be *required* — if any flow
can only be completed by typing, that flow is a bug.

So the entire on-device vocabulary is:

    UP      previous channel / move cursor up
    DOWN    next channel / move cursor down
    SELECT  open the menu / activate the highlighted item
    BACK    dismiss / go up one level

BACK is the weakest of the four and nothing may depend on it alone. No clicker has a button
for it — see the Elan mapping below, where it arrives only as a long-press — so the menu
gives every screen a `Back` item and treats the verb as a shortcut (`tuner/menu.py`).

CEC remotes and keyboards offer more than four keys, and where they do we accept the extras
(digits for direct channel entry, a dedicated power key). Nothing depends on them.

Clickers enumerate as USB HID keyboards. They don't agree on which keys they send, so the
mapping below is deliberately generous — Logitech, Kensington and the no-name ones all land
somewhere in it, which means no per-device setup.

Verified on real hardware, 2026-08-08 — an Elan 04f3:1812 wireless presenter, which has no
button labelled anything like "back":

    rectangle (top)   short  TAB 15          -> SELECT
                      long   ALT 56 + TAB    -> SELECT
    up                short  PAGEUP 104      -> UP
                      long   SHIFT 42 + F5 63 -> SELECT
    down              short  PAGEDOWN 109    -> DOWN
                      long   B 48            -> BACK
    volume up/down           115 / 114       -> VOLUME_UP / VOLUME_DOWN
    laser                    nothing at all

Every button landed in this map with no changes, and the fourth verb came from the
long-press of the down arrow — the clicker's "blank screen" function. That is precisely
what the B entry below exists for. The laser emits no event of any kind, not even a raw
scancode: it drives its diode in hardware, so it can never be a button.
"""

from __future__ import annotations

import re
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
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE = "mute"


@dataclass(frozen=True)
class Event:
    verb: Verb
    digit: int | None = None
    source: str = "unknown"


# Linux input-event key codes (linux/input-event-codes.h), used by the evdev driver.
# Generous on purpose: a clicker's "next" might be PAGEDOWN, RIGHT, DOWN or SPACE depending
# on who made it, and asking the user to identify their clicker is a setup step we refuse.
EVDEV_MAP: dict[int, Verb] = {
    109: Verb.DOWN,    # PAGEDOWN  - clicker "next", the down arrow on most models
    108: Verb.DOWN,    # DOWN
    106: Verb.DOWN,    # RIGHT
    57:  Verb.DOWN,    # SPACE     - clicker "next" on some models
    104: Verb.UP,      # PAGEUP    - clicker "previous", the up arrow
    103: Verb.UP,      # UP
    105: Verb.UP,      # LEFT
    15:  Verb.SELECT,  # TAB       - the top button on pen-style clickers, and the easiest
                       #             one to reach by feel. Worth having as SELECT.
    28:  Verb.SELECT,  # ENTER     - same button, double-pressed, on those clickers
    96:  Verb.SELECT,  # KPENTER
    63:  Verb.SELECT,  # F5        - "full screen", long-press of the up arrow
    1:   Verb.BACK,    # ESC       - "end presentation"
    14:  Verb.BACK,    # BACKSPACE
    # "Blank screen", the long-press of the down arrow. Deliberately BACK rather than
    # SELECT: a three-button clicker has no other way to produce a fourth verb, and without
    # this such a remote cannot leave a submenu.
    48:  Verb.BACK,    # B
    52:  Verb.BACK,    # DOT
    115: Verb.VOLUME_UP,
    114: Verb.VOLUME_DOWN,
    113: Verb.MUTE,
    116: Verb.POWER,
}

EVDEV_DIGITS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9, 11: 0}

# Modifiers, which are never verbs on their own and were previously discarded. They matter
# because of how a pen-style clicker expresses a long press.
#
# The two arrows swap to a different keycode entirely when held — PAGEUP becomes F5, PAGEDOWN
# becomes B — so those already arrive as distinguishable events. The top button does not: held
# or tapped it sends the same TAB, and the only difference is an ALT in front of it. Reading
# that modifier is what makes the long press of the menu button available at all.
#
# Timing is deliberately not involved. The clicker's own firmware decides what counts as a
# hold, which is why this cannot collide with the rule that a button held a beat too long
# still does the short-press action: by the time anything arrives here, the remote has already
# made that decision.
EVDEV_MODIFIERS: dict[int, str] = {
    42: "shift", 54: "shift",     # LEFTSHIFT, RIGHTSHIFT
    56: "alt", 100: "alt",        # LEFTALT, RIGHTALT
    29: "ctrl", 97: "ctrl",       # LEFTCTRL, RIGHTCTRL
}

# (modifier, keycode) -> verb. Consulted only when a modifier arrived immediately before, and
# it wins over EVDEV_MAP for that one press.
EVDEV_CHORDS: dict[tuple[str, int], Verb] = {
    ("alt", 15): Verb.POWER,      # ALT + TAB — long press of the top button on the Elan
}

# A safety net, not the mechanism. The modifier is cleared when its own key-up arrives; this
# only bounds how long a *lost* release can go on colouring the next press.
CHORD_HOLD = 5.0

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
    0x41: Verb.VOLUME_UP,
    0x42: Verb.VOLUME_DOWN,
    0x43: Verb.MUTE,
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
        import time

        # struct input_event: two longs (time), then type, code, value.
        fmt = "llHHi"
        size = struct.calcsize(fmt)

        modifier: str | None = None
        modifier_at = 0.0
        chord_fired = False

        self._file = open(self.device_path, "rb", buffering=0)
        while True:
            data = self._file.read(size)
            if not data or len(data) < size:
                break
            _, _, etype, code, value = struct.unpack(fmt, data)
            if etype != 0x01:
                continue

            # Modifiers are tracked across their whole press, so key-up matters here where it
            # matters nowhere else.
            if code in EVDEV_MODIFIERS:
                if value == 1:
                    if modifier is None:
                        modifier_at = time.monotonic()
                        chord_fired = False
                    modifier = EVDEV_MODIFIERS[code]
                elif value == 0:
                    modifier = None
                    chord_fired = False
                continue

            # Key-*down* specifically. value 2 is the kernel's autorepeat, which is what a
            # held button produces, and dropping it is what makes a long press do exactly what
            # a short press does — once. It is easy to "fix" by accepting repeats and thereby
            # make a slightly-too-long press spray the dial.
            if value != 1:
                continue

            live = modifier is not None and time.monotonic() - modifier_at < CHORD_HOLD
            if live and (modifier, code) in EVDEV_CHORDS:
                # One gesture, one action, however long it is held. Captured from the Elan:
                # a long press sends ALT down, then TAB — but keep holding and TAB arrives
                # *again*, and again, with ALT still down. Firing per TAB would toggle the
                # television off and straight back on; letting the repeats fall through to
                # EVDEV_MAP would open the menu immediately after powering off. Both are the
                # exact failure a long press is supposed to be immune to, so the repeats are
                # swallowed either way and only the first one speaks.
                if not chord_fired:
                    chord_fired = True
                    yield Event(EVDEV_CHORDS[(modifier, code)], source=self.name)
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


class MpvKeyDriver(Driver):
    """Keys captured by the mpv window itself, delivered over IPC.

    The most portable driver, and on a desktop the only one that works: evdev is Linux-only
    and needs device permissions, while mpv already owns the focused window on every
    platform. It also handles presentation clickers for free, because a clicker *is* a
    keyboard — PageUp/PageDown arrive here like any other key.

    Uses its own connection to the same IPC socket rather than sharing the player's. mpv
    accepts multiple simultaneous IPC clients, which avoids two threads racing on one socket
    and keeps tuning latency unaffected by input handling.
    """

    name = "mpv-keys"

    # mpv key name -> verb. Deliberately generous, same reasoning as the evdev map: no
    # per-device setup, no "which remote do you have" question.
    BINDINGS: dict[str, tuple[Verb, int | None]] = {
        "UP": (Verb.UP, None), "LEFT": (Verb.UP, None), "PGUP": (Verb.UP, None),
        "DOWN": (Verb.DOWN, None), "RIGHT": (Verb.DOWN, None), "PGDWN": (Verb.DOWN, None),
        "SPACE": (Verb.DOWN, None),
        "ENTER": (Verb.SELECT, None), "KP_ENTER": (Verb.SELECT, None),
        "F5": (Verb.SELECT, None),
        "b": (Verb.BACK, None), ".": (Verb.BACK, None),
        "ESC": (Verb.BACK, None), "BS": (Verb.BACK, None),
        "TAB": (Verb.SELECT, None),
        "VOLUME_UP": (Verb.VOLUME_UP, None), "VOLUME_DOWN": (Verb.VOLUME_DOWN, None),
        "MUTE": (Verb.MUTE, None),
        **{str(n): (Verb.DIGIT, n) for n in range(10)},
    }

    MESSAGE = "tub3"

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock = None

    def _connect(self, timeout: float = 10.0):
        import socket as _socket
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            try:
                sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                sock.connect(self.socket_path)
                return sock
            except OSError:
                _time.sleep(0.05)
        raise RuntimeError(f"could not connect to mpv IPC at {self.socket_path}")

    def events(self) -> Iterator[Event]:
        import json as _json

        self._sock = self._connect()
        # Register every binding. `keybind` makes mpv emit a client-message we can read,
        # which keeps key handling in one place rather than split across an input.conf.
        for key, (verb, digit) in self.BINDINGS.items():
            payload = f"{self.MESSAGE} {verb.value} {digit if digit is not None else ''}".strip()
            self._sock.sendall(
                (_json.dumps({"command": ["keybind", key, f"script-message {payload}"]}) + "\n").encode()
            )

        buffer = b""
        while True:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    message = _json.loads(line)
                except ValueError:
                    continue
                if message.get("event") != "client-message":
                    continue
                args = message.get("args") or []
                if len(args) < 2 or args[0] != self.MESSAGE:
                    continue
                try:
                    verb = Verb(args[1])
                except ValueError:
                    continue
                digit = int(args[2]) if len(args) > 2 and str(args[2]).isdigit() else None
                yield Event(verb, digit=digit, source=self.name)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


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

    # `cec-ctl --monitor` prints the *decoded* argument, not the raw byte:
    #
    #     USER_CONTROL_PRESSED (0x44):
    #             ui-cmd: channel-up (0x30)
    #     USER_CONTROL_RELEASED (0x45)
    #
    # so the code has to be taken from the parenthesised hex at the end. The obvious pattern —
    # a hex run straight after `ui-cmd:` — matches the *name* instead, and does it silently:
    # `channel-up` yields `c`, which is a real CEC code (0x0C, page up) and simply isn't in
    # the map, while `up` and `select` fail to match at all. That was the live behaviour here,
    # and its symptom is the worst kind: the TV's own remote does nothing whatsoever, with no
    # error anywhere, on a box where four other input drivers are working.
    #
    # A command cec-ctl has no name for prints as a bare decimal, hence the second pattern.
    UI_HEX = re.compile(r"ui-cmd:.*\(0x([0-9a-fA-F]{1,2})\)")
    UI_DEC = re.compile(r"ui-cmd:\s*(\d{1,3})\s*$")
    RELEASED = re.compile(r"USER_CONTROL_RELEASED")

    # A held remote button repeats USER_CONTROL_PRESSED on the wire several times a second
    # until USER_CONTROL_RELEASED arrives. Those repeats are one press, so they are dropped:
    # holding a button slightly too long does what a short press does, once. This box is
    # driven by rapid separate clicks rather than by holding, and a held CHANNEL UP racing
    # through the dial is exactly the behaviour that reads as broken.
    #
    # Not every television sends the release. So a repeat arriving after a long gap counts as
    # a fresh press, and a missed release can never wedge a button down forever.
    REPEAT_GAP = 1.2

    def __init__(self, device: str = "/dev/cec0"):
        self.device = device
        self._proc = None

    def available(self) -> bool:
        import shutil
        from pathlib import Path
        return Path(self.device).exists() and shutil.which("cec-ctl") is not None

    def events(self) -> Iterator[Event]:
        import subprocess
        import time

        self._proc = subprocess.Popen(
            ["cec-ctl", "-d", self.device, "--monitor"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        assert self._proc.stdout is not None

        held: int | None = None
        held_at = 0.0
        for line in self._proc.stdout:
            if self.RELEASED.search(line):
                held = None
                continue

            match = self.UI_HEX.search(line)
            code = int(match.group(1), 16) if match else None
            if code is None:
                match = self.UI_DEC.search(line)
                code = int(match.group(1)) if match else None
            if code is None:
                continue

            now = time.monotonic()
            if code == held and now - held_at < self.REPEAT_GAP:
                # Still the same finger on the same button. Keep the clock running, so the
                # hold stays recognised for as long as the repeats keep coming.
                held_at = now
                continue
            held, held_at = code, now

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
