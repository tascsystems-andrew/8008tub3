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
_SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<t>-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<t>-?[\d.]+)")
_SCENE_RE = re.compile(r"lavfi\.scd\.time:\s*(?P<t>[\d.]+)")

# Where act breaks fall in a typical half-hour show, as a fraction of running time.
FALLBACK_POSITIONS = (0.25, 0.55, 0.80)

# How good a candidate cut point is, by what evidence supports it. The ordering is the whole
# point: a break must never land mid-sentence, so a moment that is both dark and quiet beats
# a hard cut, which beats an arbitrary timestamp by a mile.
#
#   black + silence   a real fade to black at an act break. Unmistakable.
#   black             a fade with music over it — still an act break.
#   scene + silence   a hard cut on a quiet beat. Safe: nobody is mid-word.
#   silence           a pause. Could be dramatic rather than structural, but never mid-word.
#   scene             a hard cut with audio running. Visually clean, may clip dialogue.
#   fixed             nothing found. This is the case that cuts mid-sentence.
EVIDENCE_SCORES = {
    ("black", "silence"): 1.00,
    ("black",): 0.85,
    ("scene", "silence"): 0.75,
    ("silence",): 0.55,
    ("scene",): 0.40,
}
FIXED_SCORE = 0.20

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


def _silence_spans(info: MediaInfo) -> list[tuple[float, float]]:
    if not info.has_audio:
        return []
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-af", "silencedetect=n=-38dB:d=0.35", "-vn", "-f", "null", "-"],
        capture_stderr=True,
    )
    starts = [float(m.group("t")) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [float(m.group("t")) for m in _SILENCE_END_RE.finditer(stderr)]
    spans = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else info.duration
        if end > start:
            spans.append((max(0.0, start), end))
    return spans


def _scene_candidates(info: MediaInfo, threshold: float = 12.0) -> list[float]:
    """Hard cuts. A break landing on one is visually clean even without a fade."""
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-vf", f"scdet=threshold={threshold}", "-an", "-f", "null", "-"],
        capture_stderr=True,
    )
    return [float(m.group("t")) for m in _SCENE_RE.finditer(stderr)]


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

    head, tail = info.duration * HEAD_GUARD, info.duration * TAIL_GUARD

    blacks = [t for t in _black_candidates(info) if head < t < tail]
    silences = _silence_spans(info)
    scenes = [t for t in _scene_candidates(info) if head < t < tail]

    def _in_silence(t: float, slack: float = 0.6) -> bool:
        return any(start - slack <= t <= end + slack for start, end in silences)

    # Gather every plausible cut point with the evidence behind it. A scene change that
    # coincides with a fade is not a separate candidate — the black wins.
    scored: list[tuple[float, float, str]] = []
    for t in blacks:
        kind = ("black", "silence") if _in_silence(t) else ("black",)
        scored.append((t, EVIDENCE_SCORES[kind], "+".join(kind)))
    for t in scenes:
        if any(abs(t - b) < 1.0 for b in blacks):
            continue
        kind = ("scene", "silence") if _in_silence(t) else ("scene",)
        scored.append((t, EVIDENCE_SCORES[kind], "+".join(kind)))
    # A quiet beat with no visual change is still a safe place to break.
    for start, end in silences:
        mid = (start + end) / 2.0
        if not (head < mid < tail):
            continue
        if any(abs(mid - t) < 1.0 for t, _, _ in scored):
            continue
        scored.append((mid, EVIDENCE_SCORES[("silence",)], "silence"))

    chapters: list[Chapter] = []
    used: list[float] = []

    for fraction in FALLBACK_POSITIONS[:wanted]:
        ideal = info.duration * fraction
        tolerance = info.duration * window

        # Rank by evidence first, proximity second. A clean fade twenty seconds from the
        # ideal spot is a far better break than an arbitrary timestamp exactly on it — the
        # viewer cannot tell that a break came a little early, but they absolutely notice one
        # that arrives mid-sentence.
        nearby = [
            (t, score, why) for t, score, why in scored
            if abs(t - ideal) <= tolerance and all(abs(t - u) > 5.0 for u in used)
        ]
        if nearby:
            pick, score, why = max(
                nearby,
                key=lambda c: (c[1], -abs(c[0] - ideal) / (tolerance or 1.0)),
            )
            used.append(pick)
            drift = abs(pick - ideal) / tolerance if tolerance else 1.0
            chapters.append(Chapter(round(pick, 3), why, round(score * (1.0 - 0.15 * drift), 3)))
        else:
            # Nothing to snap to. This is the case that can land mid-sentence, so it is
            # scored low and named honestly rather than dressed up as a detection.
            chapters.append(Chapter(round(ideal, 3), "fixed", FIXED_SCORE))

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
