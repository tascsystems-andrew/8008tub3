"""Fixtures for the conditioning pass.

Four sources that between them exercise every irreversible decision `normalize` makes:

- ``quiet_43``     4:3, mastered far too quiet. Must end up pillarboxed and level-matched.
- ``loud_169``     16:9, mastered far too loud — the ad-break-blasts-the-room case.
- ``interlaced``   real interlaced fields, muxed without an honest field_order, which is
                   what a VHS capture usually looks like.
- ``letterboxed``  16:9 frame with baked-in bars, i.e. the picture is not the frame.

The loudness pair is the important one. Two files ~20 dB apart going in should come out
within a fraction of a dB of each other, because a commercial that blasts after a quiet
show is the bug that gets the whole appliance switched off.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

DURATION = 12.0


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise SystemExit("ffmpeg failed:\n  " + "\n  ".join(tail))


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    # 4:3, very quiet. -25 dB on a sine lands somewhere near -28 LUFS.
    quiet = out_dir / "quiet_43.mkv"
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"smptebars=size=640x480:rate=30000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={DURATION}",
        "-af", "volume=-25dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k", "-t", str(DURATION), str(quiet),
    ])
    made.append(quiet)

    # 16:9, very loud — the commercial that blows the doors off.
    loud = out_dir / "loud_169.mkv"
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={DURATION}",
        "-af", "volume=-3dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k", "-t", str(DURATION), str(loud),
    ])
    made.append(loud)

    # Genuinely interlaced fields. `interlace` builds them from progressive input; the
    # container is left to claim whatever it likes, which is the realistic case.
    inter = out_dir / "interlaced_43.mkv"
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=720x480:rate=60000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=520:sample_rate=48000:duration={DURATION}",
        "-vf", "interlace=scan=tff:lowpass=complex",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-flags", "+ilme+ildct",
        "-c:a", "aac", "-b:a", "128k", "-t", str(DURATION), str(inter),
    ])
    made.append(inter)

    # Baked-in letterbox: real picture is 1280x536 inside a 1280x720 frame.
    letter = out_dir / "letterboxed_169.mkv"
    _run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x536:rate=30000/1001:duration={DURATION}",
        "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={DURATION}",
        "-vf", "pad=1280:720:0:92:color=black",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k", "-t", str(DURATION), str(letter),
    ])
    made.append(letter)

    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    args = ap.parse_args()
    for path in build(args.out_dir):
        print(path)


if __name__ == "__main__":
    main()
