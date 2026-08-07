"""Does any ad break repeat a spot?

The measurement the whole dedupe exists for. Two failure modes, and they are different:

- **same file twice** — upstream picks by lifetime play count and hands every commercial in a
  block the same timestamp, so one spot really can land twice in one pod.
- **same spot, different file** — two rips of one commercial are unrelated entries upstream,
  so a break can carry both and look like a repeat to anyone watching.

Reports both, per block and across a rolling window, so "on before/off after" is a number
rather than an impression.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tub3.clusters import load_or_compute  # noqa: E402


def _epoch(text: str) -> float:
    return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def check(db: Path, station: str, ads_dir: Path, window_minutes: int) -> dict:
    clusters = load_or_compute(ads_dir).assignment

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT start_time, plan_json FROM liquid_blocks WHERE station = ? ORDER BY start_time",
        (station,),
    ).fetchall()
    conn.close()

    airings: list[tuple[float, str, int]] = []   # (epoch, realpath, cluster)
    in_block_file = 0
    in_block_cluster = 0
    blocks = 0

    for start_text, plan_json in rows:
        blocks += 1
        start = _epoch(start_text)
        elapsed = 0.0
        seen_files: set[str] = set()
        seen_clusters: set[int] = set()

        for item in json.loads(plan_json):
            duration = float(item.get("duration") or 0.0)
            if item.get("content_type") == "commercial" and item.get("path"):
                realpath = os.path.realpath(item["path"])
                cluster = clusters.get(realpath, -1)
                if realpath in seen_files:
                    in_block_file += 1
                elif cluster >= 0 and cluster in seen_clusters:
                    in_block_cluster += 1
                seen_files.add(realpath)
                if cluster >= 0:
                    seen_clusters.add(cluster)
                airings.append((start + elapsed, realpath, cluster))
            elapsed += duration

    # Rolling window: how often does a cluster recur inside `window_minutes`?
    window = window_minutes * 60.0
    last_seen: dict[int, float] = {}
    too_soon = 0
    gaps: list[float] = []
    for at, _, cluster in airings:
        if cluster < 0:
            continue
        previous = last_seen.get(cluster)
        if previous is not None:
            gap = at - previous
            gaps.append(gap)
            if gap < window:
                too_soon += 1
        last_seen[cluster] = at

    gaps.sort()

    # The floor no scheduler can beat. With C distinct spots and one airing every T seconds,
    # perfect round-robin still brings each spot back after C x T. Reporting it turns
    # "repeats within 45 minutes: 636" from an apparent failure into what it usually is — a
    # library too small for the ask. This is the number a setup wizard should show the user.
    distinct = len({c for _, _, c in airings if c >= 0})
    span = (airings[-1][0] - airings[0][0]) if len(airings) > 1 else 0.0
    mean_interval = span / max(1, len(airings) - 1) if span else 0.0
    floor_min = (distinct * mean_interval) / 60.0 if distinct and mean_interval else None

    return {
        "blocks": blocks,
        "ad_airings": len(airings),
        "distinct_spots": distinct,
        "same_file_in_block": in_block_file,
        "same_spot_in_block": in_block_cluster,
        "recur_within_window": too_soon,
        "window_minutes": window_minutes,
        "min_gap_min": round(gaps[0] / 60.0, 1) if gaps else None,
        "median_gap_min": round(gaps[len(gaps) // 2] / 60.0, 1) if gaps else None,
        "best_possible_gap_min": round(floor_min, 1) if floor_min else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path,
                    default=Path("vendor/FieldStation42/runtime/fs42_fluid.db"))
    ap.add_argument("--station", required=True)
    ap.add_argument("--ads", type=Path, required=True)
    ap.add_argument("--window", type=int, default=45)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    result = check(args.db, args.station, args.ads, args.window)
    label = f"  {args.label}" if args.label else ""
    print(f"\n{label}")
    print(f"    blocks                        {result['blocks']}")
    print(f"    commercial airings            {result['ad_airings']}")
    print(f"    same FILE twice in a break    {result['same_file_in_block']}")
    print(f"    same SPOT twice in a break    {result['same_spot_in_block']}")
    print(f"    recurred within {result['window_minutes']:>3} minutes    {result['recur_within_window']}")
    print(f"    shortest gap between repeats  {result['min_gap_min']} min")
    print(f"    median gap                    {result['median_gap_min']} min")
    print(f"    distinct spots available      {result['distinct_spots']}")
    print(f"    best gap this library allows  {result['best_possible_gap_min']} min")
    print()


if __name__ == "__main__":
    main()
