"""Group commercials that are the same spot.

Two rips of one Pepsi ad are two files. FieldStation42 rotates by lifetime play count with no
content-level comparison at all, so it treats them as two independent commercials and will
happily put both in one break. The illusion does not survive that.

adsplice already dedupes *within a single ingest run*. This does the same comparison across
the **whole commercial folder**, which is the case that matters in practice: a spot ingested
last month and the same spot ingested today are otherwise two unrelated entries forever.

The output is deliberately a cluster map rather than a delete list. Both files stay on disk —
they may be different lengths, different quality, or one may be a better transfer — and the
scheduler simply learns never to play two members of a cluster close together.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from adsplice import phash
from mediakit.ffmpeg import probe

CACHE_NAME = ".tub3-clusters.json"

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".ts", ".webm", ".wmv"}


@dataclass
class ClusterMap:
    # realpath -> cluster id
    assignment: dict[str, int]

    @property
    def clusters(self) -> int:
        return len(set(self.assignment.values()))

    def members(self) -> dict[str, list[str]]:
        """realpath -> every realpath that is the same spot, including itself."""
        by_id: dict[int, list[str]] = {}
        for path, cid in self.assignment.items():
            by_id.setdefault(cid, []).append(path)
        return {path: by_id[cid] for path, cid in self.assignment.items()}

    def duplicates(self) -> dict[int, list[str]]:
        by_id: dict[int, list[str]] = {}
        for path, cid in self.assignment.items():
            by_id.setdefault(cid, []).append(path)
        return {cid: paths for cid, paths in by_id.items() if len(paths) > 1}


def _media(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    )


def compute(
    folder: Path,
    *,
    threshold: float = 0.90,
    duration_tolerance: float = 2.0,
) -> ClusterMap:
    """Fingerprint every commercial and group the matches.

    Same guard as adsplice: clips only join a cluster if they are visually similar *and*
    close in length, so the :60 and :30 edits of one campaign stay distinct assets — which is
    correct, because a break can legitimately carry both.
    """
    paths = _media(folder)
    fingerprints: list[list[int]] = []
    durations: list[float] = []
    keep: list[Path] = []

    for path in paths:
        try:
            info = probe(path)
        except Exception:  # noqa: BLE001 - a bad file must not stop clustering
            continue
        if info.duration <= 0:
            continue
        fingerprints.append(phash.fingerprint(path, info.duration))
        durations.append(info.duration)
        keep.append(path)

    assignment: dict[str, int] = {}
    representatives: list[int] = []   # index into keep, one per cluster

    for index, fingerprint in enumerate(fingerprints):
        matched: int | None = None
        for cluster_id, rep in enumerate(representatives):
            if abs(durations[index] - durations[rep]) > duration_tolerance:
                continue
            if not fingerprints[rep] or not fingerprint:
                continue
            if phash.similarity(fingerprint, fingerprints[rep]) >= threshold:
                matched = cluster_id
                break
        if matched is None:
            matched = len(representatives)
            representatives.append(index)
        assignment[os.path.realpath(keep[index])] = matched

    return ClusterMap(assignment)


def load_or_compute(folder: Path, *, refresh: bool = False) -> ClusterMap:
    """Cache beside the commercials; recompute when the file set changes.

    Keyed on the sorted (name, size) set rather than mtimes, so a re-copied but identical
    library does not trigger a full re-fingerprint.
    """
    cache = folder / CACHE_NAME
    signature = sorted((p.name, p.stat().st_size) for p in _media(folder))
    key = json.dumps(signature)

    if cache.exists() and not refresh:
        try:
            stored = json.loads(cache.read_text())
            if stored.get("key") == key:
                return ClusterMap({k: int(v) for k, v in stored["assignment"].items()})
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    result = compute(folder)
    try:
        cache.write_text(json.dumps({"key": key, "assignment": result.assignment}, indent=2))
    except OSError:
        pass
    return result


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Group commercials that are the same spot.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    result = load_or_compute(args.folder, refresh=args.refresh)
    print(f"\n  {len(result.assignment)} spots in {result.clusters} clusters")
    dupes = result.duplicates()
    if not dupes:
        print("  no repeated spots found\n")
        return
    print(f"  {len(dupes)} spot(s) present more than once:\n")
    for paths in dupes.values():
        print(f"    {Path(paths[0]).name}")
        for other in paths[1:]:
            print(f"      = {Path(other).name}")
    print()


if __name__ == "__main__":
    main()
