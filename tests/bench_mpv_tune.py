"""End-to-end channel-change latency, through a real mpv.

`bench_tune.py` measured the demux/seek/decode cost in isolation. This measures what the
viewer actually experiences: press a button, virtual clock says where we are on the new
channel, mpv punches in there, picture appears.

Timing runs from issuing the command to mpv's ``playback-restart`` event, which is when
frames start presenting — not when the file was accepted.

The budget is 1000ms, wanted ~500ms.
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediakit.ffmpeg import probe  # noqa: E402
from tuner.player import MpvPlayer, MpvUnavailable  # noqa: E402
from tuner.schedule import LoopChannel, Lineup, Program  # noqa: E402


def build_lineup(media_dir: Path, channels: int = 5) -> Lineup:
    files = sorted(p for p in media_dir.rglob("*.mkv") if not p.name.startswith("."))
    if not files:
        raise SystemExit(f"no .mkv files under {media_dir}")

    programs = []
    for path in files:
        info = probe(path)
        if info.duration > 0:
            programs.append(Program(path, info.duration))
    if not programs:
        raise SystemExit("no probeable media")

    built = []
    for index in range(channels):
        # Rotate the playlist per channel and offset the epoch, so each channel is genuinely
        # at a different point in different content — which is what makes surfing a real test
        # rather than reloading the same file.
        rotated = programs[index % len(programs):] + programs[: index % len(programs)]
        built.append(LoopChannel(
            number=index + 2,
            name=f"CHANNEL {index + 2}",
            programs=rotated,
            epoch=-(index * 137.0),
        ))
    return Lineup(built)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("media_dir", type=Path)
    ap.add_argument("--surfs", type=int, default=24)
    ap.add_argument("--window", action="store_true", help="show the mpv window")
    args = ap.parse_args()

    lineup = build_lineup(args.media_dir)
    print(f"\n  lineup: {len(lineup.channels)} channels over "
          f"{len(lineup.channels[0].programs)} programs")  # type: ignore[attr-defined]

    player = MpvPlayer(fullscreen=False, video_output=None if args.window else "null")
    try:
        player.start()
    except MpvUnavailable as exc:
        raise SystemExit(f"mpv unavailable: {exc}")

    latencies: list[float] = []
    failures = 0
    channel = lineup.numbers[0]

    try:
        # One warm-up tune; the very first load pays for window creation and codec init,
        # which a real appliance pays once at boot rather than on a channel change.
        first = lineup.now(channel, time.time())
        if first:
            player.tune(first.program.path, first.offset)

        for step in range(args.surfs):
            channel = lineup.surf(channel, 1 if step % 3 != 2 else -1)
            airing = lineup.now(channel, time.time())
            if airing is None:
                continue
            result = player.tune(airing.program.path, airing.offset)
            if result.ok:
                latencies.append(result.latency_ms)
                print(f"  ch {airing.channel:>3}  {airing.program.name[:26]:<28}"
                      f" @{airing.offset:>7.1f}s   {result.latency_ms:>6.0f} ms")
            else:
                failures += 1
                print(f"  ch {airing.channel:>3}  FAILED: {result.error}")
    finally:
        player.close()

    if not latencies:
        raise SystemExit("no successful tunes")

    latencies.sort()
    median = statistics.median(latencies)
    p90 = latencies[int(len(latencies) * 0.9)]
    worst = latencies[-1]

    print("\n  " + "-" * 46)
    print(f"  tunes           {len(latencies)} ok, {failures} failed")
    print(f"  median          {median:>7.0f} ms")
    print(f"  p90             {p90:>7.0f} ms")
    print(f"  worst           {worst:>7.0f} ms")
    budget, want = 1000.0, 500.0
    print(f"  budget 1000ms   {'PASS' if p90 < budget else 'FAIL'} (p90)")
    print(f"  target  500ms   {'PASS' if p90 < want else 'MISS'} (p90)")
    print()


if __name__ == "__main__":
    main()
