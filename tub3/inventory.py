"""How much television do I actually have?

The question you need answered repeatedly as a library fills: is this channel ready yet?
"Ready" is not one number, and the obvious one is the wrong one.

**Hours are the weaker test. Episode count is the real one.** A channel needs roughly eight
hours before it loops audibly — but a series in a *fixed nightly slot* needs about a hundred
episodes, because a 22-episode show repeats weekly in that slot, which a viewer notices far
sooner than a channel looping. Twenty-two episodes clears the hours bar and still feels
broken. So both get reported, and the episode threshold is the one flagged.

Durations are sampled, not measured. Probing every file in a library over a network share
takes hours; probing three per series and multiplying by the count is accurate to a few
percent and takes seconds. The estimate is marked as such wherever it is shown.
"""

from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass
from pathlib import Path

from mediakit.ffmpeg import probe
from .bootstrap import VIDEO_SUFFIXES

# A fixed nightly slot repeats weekly below this, which is more noticeable than a loop.
STRIP_EPISODES = 100
# Below this a channel loops audibly within an evening.
CHANNEL_HOURS = 8.0


@dataclass
class Series:
    name: str
    path: Path
    episodes: int
    median_minutes: float
    sampled: int

    @property
    def hours(self) -> float:
        return self.episodes * self.median_minutes / 60.0

    @property
    def strip_capable(self) -> bool:
        return self.episodes >= STRIP_EPISODES

    @property
    def slot(self) -> str:
        """Which block length this series naturally fits."""
        m = self.median_minutes
        if m < 8:
            return "short"        # Looney Tunes, Schoolhouse Rock — filler and bumpers
        if m < 16:
            return "half-slot"    # 11-minute cartoons; two per half hour
        if m < 34:
            return "30min"
        if m < 62:
            return "60min"
        return "movie"


def _episodes(folder: Path) -> list[Path]:
    return [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    ]


def scan_series(folder: Path, *, samples: int = 3) -> Series | None:
    files = _episodes(folder)
    if not files:
        return None

    # Sample from across the folder rather than the first few: a series whose early seasons
    # are half-length would otherwise be mis-sized entirely.
    step = max(1, len(files) // (samples + 1))
    picks = files[step::step][:samples] or files[:1]

    durations = []
    for path in picks:
        try:
            info = probe(path)
        except Exception:  # noqa: BLE001 - one unreadable file must not sink the estimate
            continue
        if info.duration > 0:
            durations.append(info.duration / 60.0)

    if not durations:
        return None
    return Series(folder.name, folder, len(files), statistics.median(durations), len(durations))


def scan_library(root: Path, *, samples: int = 3) -> list[Series]:
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        series = scan_series(child, samples=samples)
        if series:
            out.append(series)
    return out


def arrivals(root: Path, days: float) -> list[tuple[Path, float, float]]:
    """What landed recently, newest first: (path, age in days, size in GB).

    Exists for one workflow. With a single Radarr instance, kids films land in the same
    folder as everything else and have to be moved by hand — and the hard part of that is not
    moving them, it is spotting which are new among two hundred that are not. Uses stat only,
    so it costs nothing over a network share.
    """
    import time

    cutoff = time.time() - days * 86400
    found: list[tuple[Path, float, float]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            found.append((path, (time.time() - stat.st_mtime) / 86400, stat.st_size / 1e9))
    return sorted(found, key=lambda row: row[1])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.inventory", description=__doc__)
    ap.add_argument("root", type=Path, help="a library folder containing series folders")
    ap.add_argument("--since", type=float, metavar="DAYS",
                    help="instead of an inventory, list what arrived in the last N days")
    ap.add_argument("--samples", type=int, default=3,
                    help="files probed per series for the duration estimate")
    ap.add_argument("--strip-only", action="store_true",
                    help="show only series deep enough for a fixed nightly slot")
    args = ap.parse_args(argv)

    if args.since:
        rows = arrivals(args.root, args.since)
        if not rows:
            print(f"\n  nothing new under {args.root} in the last {args.since:g} day(s)\n")
            return 0
        print(f"\n  {len(rows)} file(s) added to {args.root} in the last {args.since:g} day(s)\n")
        total = 0.0
        for path, age, gb in rows:
            total += gb
            when = f"{age * 24:.0f}h ago" if age < 1 else f"{age:.1f}d ago"
            print(f"  {when:>9}  {gb:>6.2f} GB  {path.name[:64]}")
        print(f"\n  {total:.1f} GB total\n")
        return 0

    series = scan_library(args.root, samples=args.samples)
    if not series:
        print(f"\n  nothing found under {args.root}\n")
        return 1

    if args.strip_only:
        series = [s for s in series if s.strip_capable]

    series.sort(key=lambda s: -s.hours)
    print(f"\n  {args.root}")
    print(f"  {'series':<38} {'eps':>5} {'~hours':>8} {'slot':>10}   strip")
    print("  " + "-" * 76)
    for s in series:
        mark = "yes" if s.strip_capable else f"{s.episodes}/{STRIP_EPISODES}"
        print(f"  {s.name[:37]:<38} {s.episodes:>5} {s.hours:>8.1f} {s.slot:>10}   {mark}")

    total_hours = sum(s.hours for s in series)
    strip = [s for s in series if s.strip_capable]
    print("  " + "-" * 76)
    print(f"  {len(series)} series, {sum(s.episodes for s in series):,} episodes, "
          f"~{total_hours:,.0f} hours (estimated from samples)")
    print(f"  {len(strip)} deep enough for a fixed nightly slot "
          f"({STRIP_EPISODES}+ episodes)")
    if total_hours < CHANNEL_HOURS:
        print(f"  under {CHANNEL_HOURS:.0f}h — a channel built from this loops within an evening")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
