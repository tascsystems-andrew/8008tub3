"""HDMI-CEC: drive the box from the TV's own remote, and turn the TV on.

This is the feature that decides whether the appliance feels like a television. If it works,
there is no second remote on the coffee table and no pairing step — you press the button you
were already holding.

It is also the feature most likely to fail, and every one of its failure modes is silent.
Nothing errors; the remote simply does nothing. So this module is mostly a diagnostician:
it checks each known cause and says, in plain language, what to change.

The three that account for almost all of it:

1. **CEC only works on HDMI0** — the port nearest the USB-C socket — on both Pi 4 and Pi 5.
   Support for both ports was removed deliberately. Plugged into HDMI1, CEC is simply absent.
2. **A trailing `D` on a `video=HDMI-A-1:` kernel command line forces DVI mode**, and DVI has
   no CEC line, so initialisation is skipped entirely. One character in `cmdline.txt`.
3. **An AVR or soundbar between the Pi and the TV frequently breaks CEC relay.** Widely
   reported across brands, no software fix.

One deliberate policy, and it matters more than it looks: **the box sends "turn the TV on"
only in response to a physical keypress.** Never on a timer, never on a screensaver event,
never at boot. A television that switches itself on at three in the morning is a worse
failure than any amount of setup friction.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CEC_DEVICE = "/dev/cec0"
CMDLINE = Path("/boot/firmware/cmdline.txt")
CMDLINE_LEGACY = Path("/boot/cmdline.txt")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


@dataclass
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return all(c.ok for c in self.checks if c.name in ("device", "tools"))

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def _run(args: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def diagnose() -> Diagnosis:
    result = Diagnosis()

    # --- the CEC device itself -------------------------------------------------
    device = Path(CEC_DEVICE)
    if device.exists():
        result.checks.append(Check("device", True, f"{CEC_DEVICE} present"))
    else:
        result.checks.append(Check(
            "device", False, f"{CEC_DEVICE} does not exist",
            "CEC is not initialised. On a Pi this usually means the HDMI cable is in the "
            "wrong socket — use the one NEAREST the USB-C power connector — or that "
            "dtoverlay=vc4-kms-v3d is missing from config.txt.",
        ))

    # --- tooling ---------------------------------------------------------------
    if shutil.which("cec-ctl"):
        result.checks.append(Check("tools", True, "cec-ctl available"))
    else:
        result.checks.append(Check(
            "tools", False, "cec-ctl not installed",
            "sudo apt install v4l-utils",
        ))

    # --- the one-character trap ------------------------------------------------
    cmdline = CMDLINE if CMDLINE.exists() else CMDLINE_LEGACY
    if cmdline.exists():
        try:
            text = cmdline.read_text()
        except OSError:
            text = ""
        # video=HDMI-A-1:1920x1080M@60D — the trailing D means "force DVI", and DVI carries
        # no CEC line, so the whole subsystem never starts.
        forced_dvi = re.search(r"video=HDMI-A-\d+:[^\s]*?D(\s|$)", text)
        if forced_dvi:
            result.checks.append(Check(
                "dvi-flag", False,
                f"kernel command line forces DVI mode: {forced_dvi.group(0).strip()}",
                f"Remove the trailing 'D' from that video= setting in {cmdline}. "
                "DVI has no CEC line, so this disables CEC entirely.",
            ))
        else:
            result.checks.append(Check("dvi-flag", True, "no forced-DVI flag on the cmdline"))

    # --- who else is on the bus ------------------------------------------------
    if device.exists() and shutil.which("cec-ctl"):
        code, out = _run(["cec-ctl", "-d", CEC_DEVICE, "-S"])
        if code == 0:
            names = re.findall(r"osd name\s*:\s*'([^']*)'", out, re.I)
            # Asked of the bus, not matched against the topology dump. The dump names every
            # device *type* it knows about, including this box's own — we register as logical
            # address 8, whose name is "Playback Device 2" — so a regex over that text
            # reported an audio system on every box that ever ran it, always, by matching
            # ourselves. That warning then sent a volume investigation looking for a soundbar
            # that was never there. ("Tuner" was wrong on its own terms too: a Tuner is a
            # broadcast receiver, nothing to do with audio.)
            audio = audio_system_present()
            if names:
                result.checks.append(Check("bus", True, "devices on the bus: " + ", ".join(names)))
            else:
                result.checks.append(Check(
                    "bus", False, "no other CEC devices responded",
                    "The TV may have CEC disabled. Manufacturers all rename it: Anynet+ "
                    "(Samsung), Bravia Sync (Sony), SimpLink (LG), Viera Link (Panasonic), "
                    "EasyLink (Philips). Turn it on in the TV's settings.",
                ))
            if audio:
                result.checks.append(Check(
                    "avr", True, "an audio system is on the bus — volume will go to it",
                    "Volume keys are addressed to the amplifier rather than the television "
                    "when one answers, which is what System Audio Mode expects.",
                ))
        else:
            result.checks.append(Check("bus", False, "could not scan the CEC bus", out.strip()[:160]))

    return result


def tv_on() -> bool:
    """Wake the TV and switch it to our input.

    Only ever called from a physical keypress. See the module docstring — an appliance that
    can turn a television on by itself will eventually do it at 3am.

    Delegates to tub3.cectest, which uses whichever sequence was found to work on this
    television and sends Active Source with our *real* physical address. This function
    previously hardcoded phys-addr=0.0.0.0, which is the TV's own address — announcing the
    TV as the active source tells it nothing, and is one good reason a wake can appear to
    do nothing at all.
    """
    from .cectest import wake
    return wake()


# Volume is the television's job, not the player's.
#
# mpv can attenuate its own output, and that was the first implementation — but software
# gain is the wrong instrument here. It cannot reach a soundbar the television is feeding
# over ARC, it is invisible to anyone using the TV's own remote, and mpv's ceiling is 130%,
# which is 30% above unity and clips before it gets loud. The box had in fact walked itself
# up to exactly 130 and stopped, which is what "volume up does nothing" turned out to mean.
#
# So: send the key press onward and let the set do what it does for every other input.
#
# The target is the TV rather than an audio system. The spec allows either — a source
# addresses the audio system directly when System Audio Mode is on — but on this bus address
# 5 max-retries with no acknowledgement while address 0 answers in 34ms, so there is nothing
# there to talk to. `audio_system_present` re-checks rather than assuming, because an AVR is
# exactly the sort of thing that appears later when somebody buys a soundbar.
VOLUME_UP = "volume-up"
VOLUME_DOWN = "volume-down"
VOLUME_MUTE = "mute"

TV_ADDRESS = "0"
AUDIO_ADDRESS = "5"


def audio_system_present(timeout: float = 3.0) -> bool:
    """Is there an amplifier on the bus that should own volume?

    One poll, short timeout, and a negative answer on anything unexpected. This runs on the
    way to a volume key, so it must never be the reason a button feels slow.
    """
    if not Path(CEC_DEVICE).exists() or not shutil.which("cec-ctl"):
        return False
    code, out = _run(["cec-ctl", "-d", CEC_DEVICE, "--to", AUDIO_ADDRESS,
                      "--give-osd-name"], timeout=timeout)
    return code == 0 and "Not Acknowledged" not in out


def _key(args: list[str], timeout: float) -> bool:
    if not Path(CEC_DEVICE).exists() or not shutil.which("cec-ctl"):
        return False
    code, out = _run(["cec-ctl", "-d", CEC_DEVICE] + args, timeout=timeout)
    # An unconfigured adapter makes `cec-ctl` exit 0 having transmitted nothing at all, so a
    # zero return code is not evidence on its own. Requiring the message to be named in the
    # output is what tells a real send from a silent no-op.
    return code == 0 and "Not Acknowledged" not in out and "USER_CONTROL" in out


def press_key(ui_cmd: str, to: str = TV_ADDRESS, timeout: float = 4.0) -> bool:
    """`<User Control Pressed>` alone — one step of a hold.

    Measured at 91ms against this bus, where a press carrying its release costs 155ms. That
    difference is the entire ramp speed: HDMI-CEC runs at roughly 400 bits per second, so a
    three-byte message genuinely takes a tenth of a second on the wire and no implementation
    can beat it. Sending the release only once, at the end, is both what the specification
    describes for a held key and what nearly doubles the rate.
    """
    return _key(["--to", to, "--user-control-pressed", f"ui-cmd={ui_cmd}"], timeout)


def release_key(to: str = TV_ADDRESS, timeout: float = 4.0) -> bool:
    """`<User Control Released>`, which ends a hold and must not be skipped.

    A press left unreleased leaves the television repeating the key on its own — the CEC
    equivalent of a stuck button — until it times the source out.
    """
    if not Path(CEC_DEVICE).exists() or not shutil.which("cec-ctl"):
        return False
    code, _ = _run(["cec-ctl", "-d", CEC_DEVICE, "--to", to,
                    "--user-control-released"], timeout=timeout)
    return code == 0


def send_key(ui_cmd: str, to: str = TV_ADDRESS, timeout: float = 4.0) -> bool:
    """A single tap: pressed and released, in one invocation."""
    return _key(["--to", to, "--user-control-pressed", f"ui-cmd={ui_cmd}",
                 "--user-control-released"], timeout)


def tv_off() -> bool:
    if not Path(CEC_DEVICE).exists() or not shutil.which("cec-ctl"):
        return False
    code, _ = _run(["cec-ctl", "-d", CEC_DEVICE, "--to", "0", "--standby"])
    return code == 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="tub3.cec", description=__doc__)
    ap.add_argument("action", nargs="?", default="check",
                    choices=["check", "on", "off", "watch"])
    args = ap.parse_args()

    if args.action == "on":
        print("TV on:", "sent" if tv_on() else "failed")
        return 0
    if args.action == "off":
        print("TV off:", "sent" if tv_off() else "failed")
        return 0
    if args.action == "watch":
        from tuner.input import CecDriver
        driver = CecDriver()
        if not driver.available():
            print("CEC not available — run `check` first")
            return 1
        print("Press buttons on the TV remote. Ctrl+C to stop.\n")
        try:
            for event in driver.events():
                print(f"  {event.verb.value}" + (f" {event.digit}" if event.digit is not None else ""))
        except KeyboardInterrupt:
            pass
        return 0

    result = diagnose()
    print()
    for check in result.checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name:<10} {check.detail}")
        if check.fix:
            for line in _wrap(check.fix, 68):
                print(f"           {line}")
    if not result.problems:
        print("\n  CEC looks healthy. Try: python3 -m tub3.cec watch\n")
    else:
        print(f"\n  {len(result.problems)} problem(s) above. The clicker still works "
              f"regardless — CEC is the nicety, not the requirement.\n")
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width)


if __name__ == "__main__":
    raise SystemExit(main())
