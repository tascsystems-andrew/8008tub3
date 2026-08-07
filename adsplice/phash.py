"""Perceptual hashing and deduplication.

Why this exists: FieldStation42 rotates commercials by lifetime play count with no
content-level dedupe at all, so two byte-different rips of the same spot are two
independent catalog entries and can both land in one break. Ad compilations repeat
heavily across sources. Without dedupe the same spot airs four times an hour and the
illusion collapses — this is the single highest-value patch in the whole ingest path.

Implementation is a difference hash over sampled frames, computed from raw ffmpeg output.
Zero Python dependencies, which matters because this has to run on a Pi.

Known limitation, flagged rather than hidden: this is a *visual* hash. It is robust to
compression, resolution and mild noise, but two different cutdowns of one campaign (the
60s and the 30s edit) share footage and will read as duplicates, while a tape-speed-drifted
rip of the same spot may not. The research called tempo drift out as the case that breaks
audio fingerprinting; the mitigation here is that we compare duration buckets too, so a 60s
and a 30s edit are never collapsed into each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ffmpeg import run_raw

# dhash grid: sample (W+1)xH luma pixels, compare horizontally adjacent ones.
_W, _H = 8, 8


def _frame_hash(path: Path, at: float) -> int | None:
    """Difference hash of a single frame, as a 64-bit int."""
    raw = run_raw([
        "ffmpeg", "-v", "quiet", "-ss", f"{at:.3f}", "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={_W + 1}:{_H}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ])
    if len(raw) < (_W + 1) * _H:
        return None
    bits = 0
    for row in range(_H):
        base = row * (_W + 1)
        for col in range(_W):
            bits = (bits << 1) | (1 if raw[base + col] < raw[base + col + 1] else 0)
    return bits


def fingerprint(path: Path, duration: float, samples: int = 3) -> list[int]:
    """Hash a few frames spread across the clip, avoiding the black edges."""
    if duration <= 0:
        return []
    # Sample inside the body of the clip. The first and last second of a spot are often
    # fades, which hash almost identically across completely different commercials.
    span_start = min(1.0, duration * 0.15)
    span_end = max(duration - 1.0, duration * 0.85)
    if span_end <= span_start:
        span_start, span_end = 0.0, duration

    points = [
        span_start + (span_end - span_start) * (i / max(1, samples - 1))
        for i in range(samples)
    ] if samples > 1 else [duration / 2.0]

    hashes = []
    for t in points:
        h = _frame_hash(path, t)
        if h is not None:
            hashes.append(h)
    return hashes


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def similarity(a: list[int], b: list[int]) -> float:
    """0..1 similarity between two fingerprints, best-match per frame."""
    if not a or not b:
        return 0.0
    scores = []
    for ha in a:
        best = min(hamming(ha, hb) for hb in b)
        scores.append(1.0 - best / 64.0)
    return sum(scores) / len(scores)


@dataclass
class DedupeResult:
    kept: list[int]                       # indices retained
    dropped: dict[int, int]               # dropped index -> index it duplicated
    clusters: int


def dedupe(
    fingerprints: list[list[int]],
    durations: list[float],
    *,
    threshold: float = 0.90,
    duration_tolerance: float = 2.0,
) -> DedupeResult:
    """Greedy clustering. First occurrence of a cluster wins.

    Two clips only collapse if they are visually similar *and* close in length — that keeps
    the 60s and 30s edits of one campaign as distinct assets, which is what a real break
    rotation wants.
    """
    kept: list[int] = []
    dropped: dict[int, int] = {}

    for i, fp in enumerate(fingerprints):
        if not fp:
            kept.append(i)
            continue
        match = None
        for j in kept:
            if not fingerprints[j]:
                continue
            if abs(durations[i] - durations[j]) > duration_tolerance:
                continue
            if similarity(fp, fingerprints[j]) >= threshold:
                match = j
                break
        if match is None:
            kept.append(i)
        else:
            dropped[i] = match

    return DedupeResult(kept=kept, dropped=dropped, clusters=len(kept))
