"""Normalise generated station bumpers into something a channel can actually air.

Clips arrive from an image model at whatever level and size it felt like. Measured across the
first set: -15.6 to -46.3 LUFS, a thirty decibel spread, against programme material sitting
around -21.6. Aired untouched, half of them take your head off and the other half are silent.
That is not a thing a viewer experiences as "varied" — it is experienced as broken.

So every clip is levelled to the same target as the library and letterboxed to a single size,
and the result is written to a staging folder under the names the lineup expects.

**Two-pass loudnorm, not one.** The single-pass filter adapts as it goes, which is right for a
feature and wrong for six seconds — it spends most of a short clip still deciding. Measuring
first and feeding the numbers back lets the second pass apply one linear gain across the whole
clip, so the internal dynamics survive and only the level moves.

**Scaled, not stretched.** Everything so far is 16:9 already, but a model that returns 4:3 one
day should be pillarboxed rather than distorted, so the scale is written with an explicit pad.

The naming map is deliberately explicit rather than derived from the filenames. These come
back with human names — "Far Afeild 2.mp4" — and a clever parser would silently mis-file a
clip the day one is called something unexpected. A dictionary fails loudly instead.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Measured across the library: Arthur -21.6, The Simpsons -24.0, Top Gear -22.9. The median
# is about -23, which is also EBU R128, so the library and the broadcast standard happen to
# agree and there is nothing to trade off. (The Sopranos measures -31.4, but that is a quiet
# dialogue scene in a two-minute sample rather than the level of the show.)
#
# Erring quiet is deliberate. A bumper fractionally under the programme reads as a station
# ident; a bumper over it reads as an advert, which is the single most irritating thing
# television does.
TARGET_I = -23.0
TARGET_TP = -1.5
TARGET_LRA = 11.0

WIDTH, HEIGHT = 1920, 1080

# Source filename -> staged name. `chNN-x` is picked up by that channel; `extra-brb-` is
# shared by every channel; `extra-standby-` is the off-air card, not a bumper.
NAMES = {
    "Boobtube bumper 1.mp4":            "ch03-a.mp4",
    "Boobtube bumper - were on.mp4":    "ch03-b.mp4",
    "Saturday AM Bumper.mp4":           "ch04-a.mp4",
    "Sunny Days.mp4":                   "ch05-a.mp4",
    "The Pictures Bumper.mp4":          "ch06-a.mp4",
    "Matinee.mp4":                      "ch07-a.mp4",
    "The Good Life Bumper.mp4":         "ch08-a.mp4",
    "Far Afeild 1.mp4":                 "ch09-a.mp4",
    "Far Afeild 2.mp4":                 "ch09-b.mp4",
    "Far Afeild 3.mp4":                 "ch09-c.mp4",
    "Far Afeild 4.mp4":                 "ch09-d.mp4",
    "After Dark Bumper.mp4":            "ch10-a.mp4",
    "After Dark 2 Bumper.mp4":          "ch10-b.mp4",
    "Last Laugh.mp4":                   "ch11-a.mp4",
    "Last Laugh 2 bumper.mp4":          "ch11-b.mp4",
    "The Zone.mp4":                     "ch12-a.mp4",
    "Please Stand By.mp4":              "extra-standby-a.mp4",
}


def measure(path: Path) -> dict | None:
    """Pass one: what level is this clip actually at?"""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
         "-af", f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # loudnorm prints its JSON on stderr at info level, after the usual banner noise.
    blocks = re.findall(r"\{[^{}]*?\"input_i\"[^{}]*?\}", proc.stderr, re.S)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def normalise(src: Path, dest: Path, stats: dict) -> bool:
    """Pass two: apply the measured numbers as one linear gain, and fix the size."""
    loudnorm = (
        f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
        f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}:linear=true:print_format=summary"
    )
    scale = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
             f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", scale, "-af", loudnorm,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"    ! ffmpeg: {proc.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stage_bumpers", description=__doc__)
    ap.add_argument("source", type=Path, help="folder of generated clips")
    ap.add_argument("--dest", type=Path, required=True, help="staging folder to write")
    args = ap.parse_args(argv)

    args.dest.mkdir(parents=True, exist_ok=True)
    present = {p.name for p in args.source.iterdir() if p.suffix.lower() == ".mp4"}

    unknown = sorted(present - set(NAMES))
    if unknown:
        print("\n  not in the naming map, skipped:")
        for name in unknown:
            print(f"    ? {name}")

    missing = sorted(set(NAMES) - present)
    if missing:
        print("\n  expected but not present:")
        for name in missing:
            print(f"    - {name}")

    print()
    done = 0
    for source_name, staged_name in sorted(NAMES.items(), key=lambda kv: kv[1]):
        src = args.source / source_name
        if not src.exists():
            continue
        stats = measure(src)
        if stats is None:
            print(f"  ! {source_name}: could not measure", file=sys.stderr)
            continue
        before = float(stats["input_i"])
        if not normalise(src, args.dest / staged_name, stats):
            continue
        after = measure(args.dest / staged_name)
        got = float(after["input_i"]) if after else float("nan")
        print(f"  {staged_name:<22} {before:>7.1f} -> {got:>6.1f} LUFS   {source_name}")
        done += 1

    print(f"\n  {done} clip(s) staged to {args.dest}\n")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
