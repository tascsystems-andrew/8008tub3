"""A pretend media library: a few programs and a pool of commercials.

Sized to exercise the real scheduling path rather than to look pretty:

- **Programs run 450s.** That clears FieldStation42's 300s floor for chapter detection
  (`fs42/media_processor.py`) *and* normalize's identical `PROGRAM_MIN_DURATION`, so act
  breaks are actually attempted. Below that, mid-rolls silently never happen.
- **A 600s schedule increment** leaves ~150s of gap per block, which is what forces FS42 to
  assemble a real ad pod instead of butting programs together.
- **Commercials are canonical lengths** (:15/:30/:60) so the pod packer has something
  sensible to pack, and visually distinct so perceptual dedupe has a fair test.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

PROGRAM_SECONDS = 450.0

# Cheap, low-entropy generators for the long programs. The expensive ones (mandelbrot,
# gradients) cost minutes of CPU and hundreds of megabytes at 450s, and a program only needs
# to be *distinguishable*, not interesting.
PROGRAM_SOURCES = ("smptebars", "smptehdbars", "pal75bars", "testsrc")

# Commercials are short, so visual variety is free here — and it is where variety actually
# matters, because this is what perceptual dedupe gets tested against.
AD_SOURCES = (
    "testsrc2", "smptebars", "rgbtestsrc", "yuvtestsrc",
    "testsrc", "pal75bars", "smptehdbars", "gradients",
)

PROGRAMS = ["Cop Show", "Sitcom Hour", "Cartoon Blast", "Detective Story"]
ADS = [
    ("cola", 30), ("truck", 30), ("cereal", 15), ("toys", 60),
    ("insurance", 15), ("burger", 30), ("jeans", 30), ("phone", 15),
    ("airline", 60), ("shampoo", 30), ("candy", 15), ("bank", 30),
]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-5:]
        raise SystemExit("ffmpeg failed:\n  " + "\n  ".join(tail))


def _clip(dest: Path, seed: int, seconds: float, freq: int, sources: tuple) -> None:
    if dest.exists() and dest.stat().st_size > 4096:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"{sources[seed % len(sources)]}=size=640x480:rate=30000/1001",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-g", "60",
        "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p",
        "-t", f"{seconds}", str(dest),
    ])


def build(root: Path) -> dict[str, Path]:
    shows = root / "programs"
    ads = root / "ads"

    for index, title in enumerate(PROGRAMS):
        _clip(shows / f"{title.replace(' ', '_')}.mkv", index, PROGRAM_SECONDS,
              300 + index * 90, PROGRAM_SOURCES)

    for index, (name, seconds) in enumerate(ADS):
        _clip(ads / f"{name}_{seconds}s.mkv", index, float(seconds), 480 + index * 70, AD_SOURCES)

    return {"programs": shows, "ads": ads}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    args = ap.parse_args()
    made = build(args.root)
    for label, path in made.items():
        count = len(list(path.glob("*.mkv")))
        print(f"  {label:<10} {count:>3} files  {path}")


if __name__ == "__main__":
    main()
