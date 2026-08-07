"""How long does tuning to a channel actually take?

The spec is a second at the outside, ~500ms wanted. Tuning is not a stream negotiation here
— it is "open this file and seek to where the clock says we are" — so the cost breaks down
as: open + demux probe + seek + decode to a presentable frame.

The variable that matters is **seek mode**:

- *accurate* seek decodes forward from the preceding keyframe to the exact requested frame.
  Its cost scales with how far apart the keyframes are, so a file with a 10-second GOP can
  spend seconds decoding frames nobody will ever see.
- *keyframe* (imprecise) seek jumps to the nearest keyframe and starts there. It lands up to
  half a GOP away from the requested instant — which for a TV simulation is invisible, since
  nobody knows what frame was supposed to be showing.

This measures both across several GOP lengths, which is what decides whether stream-copied
source files (whose GOP we do not control) can tune fast enough.

Process startup is included and reported separately, because it is real for a benchmark but
not for a long-running mpv that is simply told to load a new file.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from pathlib import Path

GOPS = (2, 5, 10)
CLIP_SECONDS = 300


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise SystemExit("ffmpeg failed:\n  " + "\n  ".join(tail))


def build_clips(out_dir: Path) -> dict[int, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    clips: dict[int, Path] = {}
    for gop_seconds in GOPS:
        dest = out_dir / f"gop{gop_seconds}s.mkv"
        if not dest.exists():
            frames = int(round(29.97 * gop_seconds))
            _run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i",
                f"testsrc2=size=1920x1080:rate=30000/1001:duration={CLIP_SECONDS}",
                "-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={CLIP_SECONDS}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-g", str(frames), "-keyint_min", str(frames), "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", "128k",
                "-t", str(CLIP_SECONDS), str(dest),
            ])
        clips[gop_seconds] = dest
    return clips


def measure(path: Path, offset: float, accurate: bool) -> float:
    """Seconds from launch to one decoded frame at `offset`."""
    cmd = ["ffmpeg", "-v", "error"]
    cmd += ["-accurate_seek"] if accurate else ["-noaccurate_seek"]
    cmd += ["-ss", f"{offset:.3f}", "-i", str(path),
            "-frames:v", "1", "-f", "null", "-"]
    start = time.perf_counter()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - start


def baseline() -> float:
    """ffmpeg process startup, so it can be discounted from the real player cost."""
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        subprocess.run(["ffmpeg", "-v", "quiet", "-h"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--trials", type=int, default=7)
    args = ap.parse_args()

    clips = build_clips(args.workdir)
    startup = baseline()

    # Offsets deliberately land mid-GOP, which is the worst case for accurate seek and the
    # realistic case when a virtual clock drops you at an arbitrary instant.
    offsets = [37.4, 96.7, 155.3, 211.9, 268.1]

    print(f"\n  ffmpeg process startup (discounted below): {startup * 1000:.0f} ms")
    print(f"  1920x1080 h264, {CLIP_SECONDS}s clips, {args.trials} trials per offset\n")
    print(f"  {'GOP':<8} {'seek mode':<12} {'median':>9} {'p90':>9} {'worst':>9}")
    print("  " + "-" * 52)

    verdict_rows = []
    for gop_seconds, path in clips.items():
        for accurate in (True, False):
            samples = []
            for offset in offsets:
                for _ in range(args.trials):
                    samples.append(measure(path, offset, accurate))
            adjusted = sorted(max(0.0, s - startup) for s in samples)
            median = statistics.median(adjusted)
            p90 = adjusted[int(len(adjusted) * 0.9)]
            worst = adjusted[-1]
            mode = "accurate" if accurate else "keyframe"
            print(f"  {gop_seconds:<8}s {mode:<12} {median * 1000:>7.0f}ms "
                  f"{p90 * 1000:>7.0f}ms {worst * 1000:>7.0f}ms")
            verdict_rows.append((gop_seconds, mode, median, p90))
        print()

    print("  " + "-" * 52)
    budget = 1.0
    worst_keyframe = max(p90 for _, mode, _, p90 in verdict_rows if mode == "keyframe")
    worst_accurate = max(p90 for _, mode, _, p90 in verdict_rows if mode == "accurate")
    print(f"  keyframe seek, worst p90 across all GOPs: {worst_keyframe * 1000:.0f} ms "
          f"{'PASS' if worst_keyframe < budget else 'FAIL'} (budget {budget * 1000:.0f} ms)")
    print(f"  accurate seek, worst p90 across all GOPs: {worst_accurate * 1000:.0f} ms "
          f"{'PASS' if worst_accurate < budget else 'FAIL'}")
    print()


if __name__ == "__main__":
    main()
