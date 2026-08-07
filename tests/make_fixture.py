"""Synthesise a commercial block with known ground truth.

The point of this file: the ingest pipeline has to be provable *before* anyone has real
footage, and it has to stay provable on a Pi where there is no test corpus at all. So we
generate a fake ad reel — several distinct "spots" of canonical lengths, separated by
black-and-silent gaps — and record exactly where every boundary is. The detector's output
is then scored against that, which turns "seems to work" into a precision/recall number.

Two variants matter:

- ``clean``   — true black, true silence. This is a digital compilation, the easy case.
- ``vhs``     — noise floor on both video and audio, a bright head-switching band along the
                bottom edge, and slightly jittered gap lengths. This is what an analog
                capture actually looks like, and it is the case that breaks every existing
                tool's default thresholds.

A ``duplicate`` variant repeats spots so the dedupe stage has something to catch.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

# (label, duration) — canonical broadcast lengths, deliberately mixed.
DEFAULT_SPOTS = [
    ("cola", 30.0),
    ("truck", 15.0),
    ("cereal", 30.0),
    ("toys", 60.0),
    ("insurance", 15.0),
    ("burger", 30.0),
]

GAP = 0.6  # seconds of black between spots


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise SystemExit("ffmpeg failed:\n  " + "\n  ".join(tail))


# Structurally different generators, not one generator recoloured. A hue rotation is
# invisible to a luma hash, so a fixture built that way makes every spot look like a
# duplicate and tells you nothing about dedupe. Real commercials differ in composition.
GENERATORS = (
    "testsrc2=size=640x480:rate=30",
    "smptebars=size=640x480:rate=30",
    "mandelbrot=size=640x480:rate=30",
    "life=size=640x480:rate=30:mold=10:ratio=0.1:death_color=white",
    "rgbtestsrc=size=640x480:rate=30",
    "cellauto=size=640x480:rate=30:scroll=1",
    "gradients=size=640x480:rate=30",
    "yuvtestsrc=size=640x480:rate=30",
    "smptehdbars=size=640x480:rate=30",
    "testsrc=size=640x480:rate=30",
    "pal75bars=size=640x480:rate=30",
)


def _spot_clip(out: Path, seed: int, duration: float, variant: str) -> None:
    """One synthetic commercial: distinct moving picture + distinct tone."""
    source = GENERATORS[seed % len(GENERATORS)]
    freq = 220 + seed * 110
    vf = ["format=yuv420p"]

    if variant == "vhs":
        # Analog character: grain everywhere, plus a bright head-switching band along the
        # bottom 5% that never goes black. This is the thing that defeats naive detection.
        vf = [
            "noise=alls=14:allf=t+u",
            "drawbox=x=0:y=ih*0.95:w=iw:h=ih*0.05:color=gray@0.85:t=fill",
            "format=yuv420p",
        ]

    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", source,
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration}", str(out),
    ])


def _gap_clip(out: Path, duration: float, variant: str) -> None:
    """The interstitial: black and silent, or nearly so on VHS."""
    if variant == "vhs":
        # Never truly black, never truly silent — grain plus a tape hiss floor, and the
        # head-switching band stays lit through the gap exactly as it does on real tape.
        vf = (
            "noise=alls=9:allf=t+u,"
            "drawbox=x=0:y=ih*0.95:w=iw:h=ih*0.05:color=gray@0.75:t=fill,"
            "format=yuv420p"
        )
        _run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=#0a0a0a:size=640x480:rate=30:duration={duration}",
            "-f", "lavfi", "-i", f"anoisesrc=amplitude=0.006:sample_rate=48000:duration={duration}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-t", f"{duration}", str(out),
        ])
    else:
        _run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:size=640x480:rate=30:duration={duration}",
            "-f", "lavfi", "-i", f"anullsrc=sample_rate=48000:channel_layout=stereo:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-t", f"{duration}", str(out),
        ])


def build(out_dir: Path, variant: str = "clean", duplicates: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_parts"
    work.mkdir(exist_ok=True)

    spots = list(DEFAULT_SPOTS)
    if duplicates:
        # Re-air two spots later in the reel. Same seed => same visual content, which is
        # exactly the case dedupe has to catch.
        spots = spots + [spots[0], spots[3], ("mystery", 30.0)]

    # index -> the earlier index it re-airs. A real duplicate is the *same footage* aired
    # twice, so these copy the generated file rather than regenerating it: several lavfi
    # sources (life, cellauto) are non-deterministic, and regenerating them produces
    # genuinely different content that dedupe is right to keep.
    duplicate_map = {6: 0, 7: 3} if duplicates else {}

    parts: list[Path] = []
    truth: list[dict] = []
    generated: dict[int, Path] = {}
    cursor = 0.0

    # Lead-in gap, so the reel starts on a boundary like a real tape does.
    lead = work / "gap_lead.mkv"
    _gap_clip(lead, GAP, variant)
    parts.append(lead)
    cursor += GAP

    for index, (label, duration) in enumerate(spots):
        clip = work / f"spot_{index:02d}_{label}.mkv"
        origin = duplicate_map.get(index)

        if origin is not None:
            shutil.copyfile(generated[origin], clip)
        else:
            _spot_clip(clip, index, duration, variant)
            generated[index] = clip

        parts.append(clip)
        truth.append({
            "index": index,
            "label": label,
            "start": round(cursor, 3),
            "end": round(cursor + duration, 3),
            "duration": duration,
            "duplicate_of": origin,
        })
        cursor += duration

        gap = work / f"gap_{index:02d}.mkv"
        _gap_clip(gap, GAP, variant)
        parts.append(gap)
        cursor += GAP

    concat_list = work / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))

    block = out_dir / f"block_{variant}{'_dupes' if duplicates else ''}.mkv"
    _run([
        "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(block),
    ])

    manifest = {
        "block": str(block),
        "variant": variant,
        "gap": GAP,
        "total_duration": round(cursor, 3),
        "spots": truth,
        "boundaries": sorted({round(s["start"], 3) for s in truth} | {round(s["end"], 3) for s in truth}),
    }
    (out_dir / f"truth_{variant}{'_dupes' if duplicates else ''}.json").write_text(
        json.dumps(manifest, indent=2)
    )
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic commercial block with ground truth.")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--variant", choices=["clean", "vhs"], default="clean")
    ap.add_argument("--duplicates", action="store_true")
    args = ap.parse_args()

    manifest = build(args.out_dir, args.variant, args.duplicates)
    print(f"{manifest['block']}  ({manifest['total_duration']}s, {len(manifest['spots'])} spots)")


if __name__ == "__main__":
    main()
