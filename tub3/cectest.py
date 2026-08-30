"""Find out what your television actually responds to, instead of guessing.

Turning a TV **off** is one command that nearly everything implements. Turning one **on** is
not a command at all — it is a sequence, and manufacturers disagree about which. Some wake
on `Image View On`, some only on an `Active Source` broadcast, some want both in that order
with a pause between, some want `Text View On` instead, and some ignore CEC entirely when
in deep standby because a power-saving setting cut the standby voltage to the CEC line.

So a box that hardcodes one sequence works on some televisions and, on the rest, turns off
reliably and never turns on — which is the exact complaint people have about every streaming
stick that ships a single hardcoded guess.

The way out is not a better guess. It is measurement:

**`Give Device Power Status` gives ground truth.** The TV reports on / standby / in
transition. So each strategy can be tried and then *verified*, and the box can find the one
that works on this television and remember it. No user is asked to know what Image View On
is; they are asked to watch the screen, and mostly not even that.

**Every command is individually testable**, because a sequence that fails should be
debuggable down to which step the TV ignored.

Two behaviours are deliberately never configurable:

- **The box only ever turns a TV on in response to a physical keypress.** Never a timer,
  never at boot. A television that switches itself on at three in the morning is a worse
  fault than any amount of setup friction.
- **Nothing here is tried automatically at startup.** Probing wake strategies means turning
  the television on repeatedly; it happens when someone asks for it and is watching.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CEC_DEVICE = "/dev/cec0"
SETTINGS = Path(__file__).resolve().parent.parent / "cec.json"

# 0 = on, 1 = standby, 2 = transition to on, 3 = transition to standby.
POWER_NAMES = {0: "on", 1: "standby", 2: "waking", 3: "going to standby"}


@dataclass
class Result:
    ok: bool
    detail: str
    power_before: str | None = None
    power_after: str | None = None
    raw: str = ""


_configured = False


def ensure_configured(force: bool = False) -> tuple[bool, str]:
    """Claim a logical address on the CEC bus, without which nothing answers.

    A freshly-opened `/dev/cec0` has `Logical Address Mask: 0x0000` — it is not a
    participant, only a wire. It can transmit, but no device will ever reply, so every
    addressed command silently goes nowhere and every "is the TV on?" query returns
    nothing at all.

    That is a nasty failure because it is not symmetrical. Broadcast-ish commands like
    Standby often still land, so the box turns the television *off* perfectly and cannot
    turn it *on* or read its state — which is exactly the behaviour people describe on
    streaming boxes whose CEC "only half works".

    Registering as a Playback device fixes it and has a second benefit: the OSD name is
    what the television lists on its input menu, so the box appears as BoobTube rather
    than as a blank entry.

    Survives until reboot, so this is called before the first command and then remembered.
    """
    global _configured
    if _configured and not force:
        return True, "already configured"
    if not Path(CEC_DEVICE).exists() or not shutil.which("cec-ctl"):
        return False, "no CEC device or cec-ctl"

    code, out = _run(["cec-ctl", "-d", CEC_DEVICE, "--playback",
                      "--osd-name", "BoobTube"], timeout=12.0)
    match = re.search(r"Logical Address Mask\s*:\s*(0x[0-9a-fA-F]+)", out)
    claimed = bool(match and match.group(1) != "0x0000")
    _configured = claimed
    if claimed:
        # Built outside the f-string: Python 3.11 rejects a backslash inside one.
        address = re.search(r"Logical Address\s*:\s*(\d+)", out)
        which = address.group(1) if address else "a playback device"
        return True, f"registered as logical address {which}"
    return False, ("could not claim a CEC logical address — the bus may be busy or the "
                   "TV's CEC support switched off")


def available() -> tuple[bool, str]:
    if not Path(CEC_DEVICE).exists():
        return False, (f"{CEC_DEVICE} does not exist — CEC is not initialised. On a Pi the "
                       f"usual cause is the HDMI cable being in the wrong socket: use the "
                       f"one nearest the USB-C power connector.")
    if not shutil.which("cec-ctl"):
        return False, "cec-ctl is not installed (sudo apt install v4l-utils)"
    return True, "ready"


def _run(args: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def physical_address() -> str:
    """Our own address on the HDMI tree, e.g. "2.0.0.0".

    This matters more than it looks. `Active Source` announces *which* input should be
    shown, by physical address, and the previous code hardcoded 0.0.0.0 — which is the
    TV's own address, not ours. A TV told that the TV is now the active source has been
    told nothing, which is one good reason a wake attempt would appear to do nothing at
    all. The adapter learns the real address from the EDID; ask it.
    """
    code, out = _run(["cec-ctl", "-d", CEC_DEVICE])
    if code == 0:
        match = re.search(r"Physical Address\s*:\s*([0-9a-fA-F](?:\.[0-9a-fA-F]){3})", out)
        if match:
            return match.group(1)
    return "1.0.0.0"


def power_status(timeout: float = 6.0) -> tuple[str | None, str]:
    """Ask the TV whether it is on. Ground truth, and the whole basis of the probe."""
    ensure_configured()
    code, out = _run(["cec-ctl", "-d", CEC_DEVICE, "--to", "0",
                      "--give-device-power-status"], timeout=timeout)
    match = re.search(r"pwr-state:\s*(\w[\w\- ]*)", out)
    if match:
        name = match.group(1).strip().lower()
        if "standby" in name and "transition" not in name:
            return "standby", out
        if name.startswith("on"):
            return "on", out
        return name, out
    # Some adapters report the numeric form instead.
    match = re.search(r"power status[^0-9]*(\d)", out, re.I)
    if match:
        return POWER_NAMES.get(int(match.group(1)), "unknown"), out
    return (None, out) if code != 0 else (None, out)


# Every individual thing worth sending, in plain language. The label is what a person sees
# on a button; the note says what it is for, because "Text View On" means nothing to anyone
# who has not read the HDMI spec.
COMMANDS: dict[str, dict] = {
    "image-view-on": {
        "label": "Wake (Image View On)",
        "note": "The standard wake command. Most TVs implement it; some ignore it.",
        "args": ["--to", "0", "--image-view-on"],
    },
    "text-view-on": {
        "label": "Wake (Text View On)",
        "note": "An alternative wake some sets answer when Image View On is ignored.",
        "args": ["--to", "0", "--text-view-on"],
    },
    "active-source": {
        "label": "Switch to this input",
        "note": "Announces us as the active source. Several TVs treat this alone as a "
                "wake, and it is what actually changes the input.",
        "args": ["--active-source", "phys-addr={phys}"],
    },
    "power-on-function": {
        "label": "Remote: power-on button",
        "note": "Imitates the power-on key on the TV's own remote. Works on some sets "
                "that ignore both wake commands.",
        "args": ["--to", "0", "--user-control-pressed", "ui-cmd=power-on-function"],
    },
    "power-toggle": {
        "label": "Remote: power toggle",
        "note": "The toggle key. Last resort — if the TV is already on, this turns it off.",
        "args": ["--to", "0", "--user-control-pressed", "ui-cmd=power"],
    },
    "standby": {
        "label": "Turn the TV off",
        "note": "Nearly universal. This is the direction that always works.",
        "args": ["--to", "0", "--standby"],
    },
    "power-status": {
        "label": "Ask if the TV is on",
        "note": "Reads the TV's reported power state. Costs nothing and changes nothing.",
        "args": ["--to", "0", "--give-device-power-status"],
    },
}

# Ordered by how likely they are to work and how disruptive they are if they do not.
# power-toggle is last because on a TV that is already on it turns it off.
WAKE_STRATEGIES: list[dict] = [
    {"key": "image+active", "label": "Image View On, then switch input",
     "steps": ["image-view-on", "active-source"]},
    {"key": "active", "label": "Switch input only",
     "steps": ["active-source"]},
    {"key": "text+active", "label": "Text View On, then switch input",
     "steps": ["text-view-on", "active-source"]},
    {"key": "power-on", "label": "Remote power-on key, then switch input",
     "steps": ["power-on-function", "active-source"]},
    {"key": "toggle", "label": "Remote power toggle, then switch input",
     "steps": ["power-toggle", "active-source"]},
]


def load() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save(config: dict) -> dict:
    current = load()
    current.update(config)
    SETTINGS.write_text(json.dumps(current, indent=2))
    return current


def send(name: str, *, gap: float = 0.0) -> Result:
    """One command, on its own, so a failing sequence can be debugged step by step."""
    ok, why = available()
    if not ok:
        return Result(False, why)
    # Claim a logical address first. It does not survive a reboot, and without it the
    # adapter is a wire rather than a participant: commands transmit, nothing ever replies.
    # Doing it here rather than at startup means it is impossible to forget, and it costs
    # one subprocess on the first CEC use per boot.
    ensure_configured()
    spec = COMMANDS.get(name)
    if spec is None:
        return Result(False, f"unknown command {name!r}")

    args = [a.format(phys=physical_address()) for a in spec["args"]]
    code, out = _run(["cec-ctl", "-d", CEC_DEVICE, *args])
    if gap:
        time.sleep(gap)
    # cec-ctl exits 0 for "transmitted"; whether the TV *did* anything is a separate
    # question, which is what the probe below is for.
    return Result(code == 0, "sent" if code == 0 else "the adapter refused to send it",
                  raw=out.strip()[:400])


def run_strategy(key: str, *, gap: float = 1.2) -> Result:
    """Send a whole wake sequence and report what the TV's power state did.

    The gap matters. A TV that is waking will drop commands sent while it is still coming
    up, which is the commonest reason a sequence works by hand and fails in code.
    """
    strategy = next((s for s in WAKE_STRATEGIES if s["key"] == key), None)
    if strategy is None:
        return Result(False, f"unknown strategy {key!r}")

    before, _ = power_status()
    for index, step in enumerate(strategy["steps"]):
        result = send(step)
        if not result.ok:
            return Result(False, f"step {index + 1} ({step}) could not be sent",
                          power_before=before, raw=result.raw)
        time.sleep(gap)

    # Give the set a moment; some report "waking" before they report "on".
    after = before
    for _ in range(6):
        after, _ = power_status()
        if after == "on":
            break
        time.sleep(1.0)

    woke = after == "on" and before != "on"
    if after == "on" and before == "on":
        detail = "the TV was already on, so this proves nothing — turn it off and retry"
    elif woke:
        detail = "the TV turned on"
    else:
        detail = f"the TV did not turn on (it reports {after or 'nothing'})"
    return Result(woke, detail, power_before=before, power_after=after)


def probe(gap: float = 1.2) -> dict:
    """Try each strategy until one demonstrably works, and remember it.

    Stops at the first success rather than testing all of them: every attempt turns the
    television on, and there is nothing to learn from the rest once one works.
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}

    before, _ = power_status()
    if before == "on":
        return {"ok": False, "error": "The TV is already on. Turn it off first — a wake "
                                      "test against a set that is already awake cannot "
                                      "tell you anything."}

    attempts = []
    for strategy in WAKE_STRATEGIES:
        result = run_strategy(strategy["key"], gap=gap)
        attempts.append({"key": strategy["key"], "label": strategy["label"],
                         "ok": result.ok, "detail": result.detail})
        if result.ok:
            save({"wake": strategy["key"], "gap": gap})
            return {"ok": True, "found": strategy["key"], "label": strategy["label"],
                    "attempts": attempts}
        # Put it back to standby before the next attempt, or every later strategy meets a
        # TV that is already on and the result is meaningless.
        send("standby")
        time.sleep(2.0)

    return {"ok": False, "attempts": attempts,
            "error": "None of the wake sequences turned the TV on. Two things worth "
                     "checking on the TV itself: that CEC is enabled (every manufacturer "
                     "renames it — Anynet+, Bravia Sync, SimpLink, Viera Link, EasyLink), "
                     "and that its power-saving or eco mode is not cutting power to the "
                     "CEC line in standby, which no software can work around."}


def await_on(timeout: float = 8.0, poll: float = 0.6) -> bool:
    """Wait until the set says it is on, or give up.

    A television coming out of standby answers CEC long before it will act on it. Asking
    rather than guessing is the difference between an input switch that lands and one the
    set drops while its panel is still warming.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, _ = power_status(timeout=3.0)
        if state == "on":
            return True
        time.sleep(poll)
    return False


def wake() -> bool:
    """Turn the TV on and take the input. Keypress-driven only.

    The input half is the part that used to go missing. The wake and the input switch are two
    separate messages, and the second was sent a fixed 1.2s after the first — which is plenty
    for a set that is already awake and not nearly enough for one coming out of standby, so
    the television came on and stayed on whatever it was showing before.

    So Active Source now waits for the set to *report* itself on rather than counting
    seconds, and is sent again afterwards. The repeat is free — Active Source is a broadcast
    announcement, not a request, and a set that already agrees ignores it — and it covers the
    case where the first one lands during the last moment of warm-up and is discarded.
    """
    config = load()
    key = config.get("wake")
    if not key:
        # Nothing configured yet: the most widely implemented sequence.
        key = "image+active"
    strategy = next((s for s in WAKE_STRATEGIES if s["key"] == key), WAKE_STRATEGIES[0])
    gap = float(config.get("gap", 1.2))
    for step in strategy["steps"]:
        if step == "active-source":
            # Do not announce the source to a set that is not listening yet.
            await_on(timeout=float(config.get("wake_timeout", 8.0)))
        if not send(step).ok:
            return False
        time.sleep(gap)

    # Say it once more. Cheap, harmless, and the difference on a slow panel.
    send("active-source")
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="tub3.cectest", description=__doc__)
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "probe", "wake", "off", "send", "list"])
    ap.add_argument("--command", help="for 'send': which one (see 'list')")
    ap.add_argument("--gap", type=float, default=1.2,
                    help="seconds between steps; raise it if the TV drops commands "
                         "while it is waking")
    args = ap.parse_args(argv)

    ok, why = available()
    if not ok and args.action != "list":
        print(f"\n  {why}\n")
        return 1

    if args.action == "list":
        print()
        for name, spec in COMMANDS.items():
            print(f"  {name:<20} {spec['label']}")
            print(f"  {'':<20} {spec['note']}")
        print()
        return 0

    if args.action == "status":
        state, _ = power_status()
        config = load()
        print(f"\n  adapter      {CEC_DEVICE}")
        print(f"  our address  {physical_address()}")
        print(f"  TV reports   {state or 'no answer'}")
        chosen = config.get("wake") or "not configured (will try image+active)"
        print(f"  wake using   {chosen}")
        print()
        return 0

    if args.action == "probe":
        print("\n  Trying each wake sequence. The TV will switch on and off a few times.\n")
        outcome = probe(gap=args.gap)
        for attempt in outcome.get("attempts", []):
            mark = "works" if attempt["ok"] else "no"
            print(f"    {mark:<6} {attempt['label']:<42} {attempt['detail']}")
        print()
        if outcome.get("ok"):
            print(f"  Using: {outcome['label']}  (saved)\n")
            return 0
        print(f"  {outcome.get('error', 'no sequence worked')}\n")
        return 1

    if args.action == "wake":
        print("TV on:", "sent" if wake() else "failed")
        return 0
    if args.action == "off":
        print("TV off:", "sent" if send("standby").ok else "failed")
        return 0

    if args.action == "send":
        if not args.command:
            print("\n  --command is required (see 'list')\n")
            return 1
        result = send(args.command)
        print(f"\n  {args.command}: {result.detail}")
        if result.raw:
            print(f"  {result.raw.splitlines()[0][:120]}")
        state, _ = power_status()
        print(f"  TV now reports: {state or 'no answer'}\n")
        return 0 if result.ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
