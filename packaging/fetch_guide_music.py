"""Fetch and level the guide channel's music.

The listings channel plays a bed under the grid. Sources are given as URLs and pulled with
yt-dlp, which is the only part of this that needs the network — everything after is the same
treatment the bumpers get, and for the same reason.

**Levelled to match the dial.** A track pulled from the web arrives at whatever level it was
uploaded at, and the guide is one button away from a programme. The bumpers measured -15.6 to
-46.3 LUFS across a single delivery; music is no better behaved. Everything lands at the same
target as the rest of the box so that pressing 2 does not change how loud the room is.

**Kept as one long file per source.** These are hour-plus compilations, and a compilation is
already a playlist — splitting it on silence would produce a hundred fragments whose
boundaries are worse than the ones the uploader chose. mpv loops the file.

Runs on the Mac, not the Pi: yt-dlp lives here, the Pi deliberately gets mpv and the standard
library. Output is copied over afterwards, exactly like the bumpers.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Same target as the bumpers and the library median, so the guide is not a step up or down
# from whatever channel you just left.
TARGET_I = -23.0
TARGET_TP = -1.5
TARGET_LRA = 11.0


def have(binary: str) -> bool:
    return subprocess.run(["which", binary], capture_output=True).returncode == 0


def fetch(url: str, into: Path, name: str) -> Path | None:
    """Pull the audio track only. Returns the downloaded file."""
    into.mkdir(parents=True, exist_ok=True)
    template = str(into / f"{name}.%(ext)s")
    proc = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "m4a", "--audio-quality", "0",
         "--no-playlist", "--no-progress", "-o", template, url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        print(f"  ! {url}: {' / '.join(tail)}", file=sys.stderr)
        return None
    found = sorted(into.glob(f"{name}.*"))
    return found[0] if found else None


def measure(path: Path) -> dict | None:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
         "-af", f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    blocks = re.findall(r"\{[^{}]*?\"input_i\"[^{}]*?\}", proc.stderr, re.S)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def level(src: Path, dest: Path, stats: dict) -> bool:
    """Two-pass loudnorm: measured numbers back in, one linear gain out."""
    loudnorm = (
        f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
        f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}:linear=true:print_format=summary"
    )
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", loudnorm,          # 128k, not the 192k the bumpers get. This is hours of background instrumental
         # played under a text grid, where the difference is inaudible and the size is not:
         # eleven hours at 192k is most of a gigabyte on an SD card.
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  ! ffmpeg: {proc.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fetch_guide_music", description=__doc__)
    ap.add_argument("--dest", type=Path, required=True,
                    help="guide music folder; a Christmas/ subfolder is made as needed")
    ap.add_argument("--url", action="append", default=[], help="ordinary bed, repeatable")
    ap.add_argument("--christmas", action="append", default=[],
                    help="festive bed (15 Nov - 1 Jan), repeatable")
    args = ap.parse_args(argv)

    if not have("yt-dlp"):
        print("error: yt-dlp not found (brew install yt-dlp)", file=sys.stderr)
        return 2
    if not (args.url or args.christmas):
        print("error: nothing to fetch", file=sys.stderr)
        return 2

    work = args.dest / ".raw"
    jobs = [(u, args.dest, f"guide-{i:02d}") for i, u in enumerate(args.url, 1)]
    jobs += [(u, args.dest / "Christmas", f"christmas-{i:02d}")
             for i, u in enumerate(args.christmas, 1)]

    done = 0
    for url, folder, name in jobs:
        print(f"\n  {name}  {url}")
        raw = fetch(url, work, name)
        if raw is None:
            continue
        stats = measure(raw)
        if stats is None:
            print("  ! could not measure", file=sys.stderr)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{name}.m4a"
        if not level(raw, dest, stats):
            continue
        after = measure(dest)
        got = float(after["input_i"]) if after else float("nan")
        size = dest.stat().st_size / 1e6
        print(f"    {float(stats['input_i']):.1f} -> {got:.1f} LUFS   {size:.0f} MB   {dest}")
        raw.unlink(missing_ok=True)
        done += 1

    if work.is_dir() and not any(work.iterdir()):
        work.rmdir()
    print(f"\n  {done}/{len(jobs)} track(s) ready in {args.dest}\n")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
