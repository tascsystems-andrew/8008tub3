"""Synthetic act-break chapters.

FieldStation42 places mid-roll ad pods at chapter markers, and library files don't have
any. Without them, breaks can only land between programs — which is the single most
obvious tell that you're watching a playlist rather than television.

So: find the act breaks. Most 90s TV fades to black at them, so `blackdetect` over the
episode gives real candidates. Where it doesn't, fall back to fixed proportions — a
22-minute sitcom reliably breaks near a quarter, a half and three-quarters through.

Chapters are written as ffmetadata during the encode that's happening anyway, so this
costs nothing extra.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mediakit.ffmpeg import MediaInfo, run

_BLACK_RE = re.compile(r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)")

# Where act breaks fall in a typical half-hour show, as a fraction of running time.
FALLBACK_POSITIONS = (0.25, 0.55, 0.80)

# Don't place a break in the first or last stretch of a program — nobody cuts to
# commercial ninety seconds before the credits.
HEAD_GUARD = 0.10
TAIL_GUARD = 0.92


@dataclass(frozen=True)
class Chapter:
    time: float
    method: str        # "blackdetect" | "fallback" | "source"
    confidence: float


def _black_candidates(info: MediaInfo) -> list[float]:
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-vf", "blackdetect=d=0.25:pix_th=0.12:pic_th=0.96",
         "-an", "-f", "null", "-"],
        capture_stderr=True,
    )
    out = []
    for m in _BLACK_RE.finditer(stderr):
        start, end = float(m.group("start")), float(m.group("end"))
        out.append((start + end) / 2.0)
    return out


def find_act_breaks(
    info: MediaInfo,
    *,
    wanted: int = 3,
    window: float = 0.09,
) -> list[Chapter]:
    """Pick act breaks, preferring real fades to black near the expected positions."""
    if info.duration <= 0:
        return []

    # A file that already carries chapters is authoritative — don't second-guess it.
    if info.chapters:
        usable = [
            c for c in info.chapters
            if info.duration * HEAD_GUARD < c < info.duration * TAIL_GUARD
        ]
        if usable:
            return [Chapter(round(c, 3), "source", 1.0) for c in sorted(usable)]

    candidates = [
        t for t in _black_candidates(info)
        if info.duration * HEAD_GUARD < t < info.duration * TAIL_GUARD
    ]

    chapters: list[Chapter] = []
    used: set[float] = set()

    for fraction in FALLBACK_POSITIONS[:wanted]:
        ideal = info.duration * fraction
        tolerance = info.duration * window

        nearby = [
            t for t in candidates
            if abs(t - ideal) <= tolerance and t not in used
        ]
        if nearby:
            pick = min(nearby, key=lambda t: abs(t - ideal))
            used.add(pick)
            # Confidence falls off with distance from where the break "should" be.
            drift = abs(pick - ideal) / tolerance if tolerance else 1.0
            chapters.append(Chapter(round(pick, 3), "blackdetect", round(1.0 - 0.4 * drift, 3)))
        else:
            chapters.append(Chapter(round(ideal, 3), "fallback", 0.35))

    return sorted(chapters, key=lambda c: c.time)


def to_ffmetadata(chapters: list[Chapter], duration: float) -> str:
    """Render chapters as an ffmetadata file.

    Chapters are spans, not points, so each marker opens a section that runs to the next
    one. Times are in milliseconds.
    """
    lines = [";FFMETADATA1"]
    edges = [0.0] + [c.time for c in chapters] + [duration]
    for i in range(len(edges) - 1):
        start_ms = int(edges[i] * 1000)
        end_ms = max(start_ms + 1, int(edges[i + 1] * 1000) - 1)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title=Act {i + 1}",
        ]
    return "\n".join(lines) + "\n"
