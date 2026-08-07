"""Ad-boundary detection.

A real break between two spots is black *and* silent. Requiring both is what keeps a
fade-to-black inside a commercial from being mistaken for the end of one.

Two things here are not in any existing tool, and both come straight from the research:

1. **VHS pre-crop.** Analog captures carry a head-switching noise band along the bottom
   edge and garbage in overscan. Those pixels are never black, so a whole-frame black
   detector scored against them silently misses every boundary on a VHS source. We crop
   before measuring.

2. **The duration prior as a scoring signal, not a filter.** Commercials are cut to 15,
   30 and 60 seconds. Every existing detector uses duration as a post-hoc reject rule at
   best. We score candidate cuts by how close the resulting segments land to canonical
   lengths, which lets a weak black/silence signal still produce a confident cut when the
   arithmetic agrees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ffmpeg import MediaInfo, run

# Canonical broadcast spot lengths, with a prior weight for how common each actually is.
#
# The weights matter more than they look. Real inventory is overwhelmingly :15/:30/:60 —
# :20 and :45 exist but are rare. Scoring them equally lets a *fragment* of a longer spot
# land near a rare slot and pass as a whole commercial, which defeats the recombination
# pass downstream. Weighting by real-world frequency is what lets "these two pieces sum to
# exactly 60" outrank "this piece is roughly 20".
CANONICAL_DURATIONS: tuple[tuple[float, float], ...] = (
    (30.0, 1.00),
    (15.0, 1.00),
    (60.0, 0.97),
    (120.0, 0.80),
    (10.0, 0.72),
    (20.0, 0.68),
    (45.0, 0.66),
    (5.0, 0.55),
)

_BLACK_RE = re.compile(
    r"black_start:(?P<start>[\d.]+)\s+black_end:(?P<end>[\d.]+)\s+black_duration:(?P<dur>[\d.]+)"
)
_SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<t>-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<t>-?[\d.]+)")


@dataclass(frozen=True)
class Profile:
    """Detection thresholds. Analog sources need looser everything."""

    name: str
    # blackdetect
    black_min_duration: float
    pixel_threshold: float   # pix_th: fraction of luminance below which a pixel is "black"
    picture_threshold: float  # pic_th: fraction of pixels that must be black
    # silencedetect
    silence_db: float
    silence_min_duration: float
    # pre-crop, as a fraction of each edge, to drop head-switching noise and overscan
    crop_bottom: float = 0.0
    crop_edges: float = 0.0

    @property
    def crops(self) -> bool:
        return self.crop_bottom > 0 or self.crop_edges > 0


DIGITAL = Profile(
    name="digital",
    black_min_duration=0.05,
    pixel_threshold=0.10,
    picture_threshold=0.98,
    silence_db=-45.0,
    silence_min_duration=0.20,
)

# VHS: the tape noise floor never reaches true black or true silence. Every threshold
# loosens, and the bottom 6% of the frame is discarded before measuring — that band is
# where head-switching noise lives and it is never dark.
VHS = Profile(
    name="vhs",
    black_min_duration=0.04,
    pixel_threshold=0.28,
    picture_threshold=0.90,
    silence_db=-32.0,
    silence_min_duration=0.15,
    crop_bottom=0.06,
    crop_edges=0.02,
)

PROFILES = {"digital": DIGITAL, "vhs": VHS}


@dataclass(frozen=True)
class Interval:
    start: float
    end: float

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def contains(self, t: float, slack: float = 0.0) -> bool:
        return (self.start - slack) <= t <= (self.end + slack)


@dataclass
class Boundary:
    """A candidate cut point between two spots."""

    time: float
    black: Interval | None = None
    silence: Interval | None = None
    # 0..1, how much the two signals agree
    agreement: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def both_signals(self) -> bool:
        return self.black is not None and self.silence is not None


def _crop_filter(info: MediaInfo, profile: Profile) -> str | None:
    if not profile.crops or not info.width or not info.height:
        return None
    dx = int(info.width * profile.crop_edges)
    dy_top = int(info.height * profile.crop_edges)
    dy_bottom = int(info.height * profile.crop_bottom)
    w = info.width - 2 * dx
    h = info.height - dy_top - dy_bottom
    if w <= 16 or h <= 16:
        return None
    return f"crop={w}:{h}:{dx}:{dy_top}"


def find_black(info: MediaInfo, profile: Profile) -> list[Interval]:
    chain = []
    crop = _crop_filter(info, profile)
    if crop:
        chain.append(crop)
    chain.append(
        f"blackdetect=d={profile.black_min_duration}"
        f":pix_th={profile.pixel_threshold}"
        f":pic_th={profile.picture_threshold}"
    )
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-vf", ",".join(chain), "-an", "-f", "null", "-"],
        capture_stderr=True,
    )
    return [
        Interval(float(m.group("start")), float(m.group("end")))
        for m in _BLACK_RE.finditer(stderr)
    ]


def find_silence(info: MediaInfo, profile: Profile) -> list[Interval]:
    if not info.has_audio:
        return []
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-af", f"silencedetect=n={profile.silence_db}dB:d={profile.silence_min_duration}",
         "-vn", "-f", "null", "-"],
        capture_stderr=True,
    )
    starts = [float(m.group("t")) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [float(m.group("t")) for m in _SILENCE_END_RE.finditer(stderr)]
    intervals: list[Interval] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else info.duration
        if end > start:
            intervals.append(Interval(max(0.0, start), end))
    return intervals


def _overlap(a: Interval, b: Interval) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def find_boundaries(
    info: MediaInfo,
    profile: Profile,
    *,
    slack: float = 0.35,
) -> list[Boundary]:
    """Black periods whose midpoint falls inside (or within `slack` of) a silence period.

    Falling back to black-only is deliberate: a silent-but-not-black or black-but-not-silent
    candidate still gets through, just with lower agreement, and the confidence model
    downstream decides whether to keep the resulting segment. Nothing is queued for a human.
    """
    blacks = find_black(info, profile)
    silences = find_silence(info, profile)

    boundaries: list[Boundary] = []
    for black in blacks:
        match = next((s for s in silences if s.contains(black.mid, slack)), None)
        if match is None:
            boundary = Boundary(time=black.mid, black=black, agreement=0.35)
            boundary.notes.append("black without silence")
        else:
            ov = _overlap(black, match)
            # Agreement rewards a silence that actually brackets the black period.
            ratio = ov / black.duration if black.duration else 0.0
            boundary = Boundary(
                time=black.mid,
                black=black,
                silence=match,
                agreement=min(1.0, 0.6 + 0.4 * ratio),
            )
        boundaries.append(boundary)

    # Silence with no black at all is a weak signal on its own — a quiet beat inside a spot
    # looks identical. Only admit it when it is long enough to be a real interstitial.
    for silence in silences:
        if any(b.silence is silence for b in boundaries):
            continue
        if silence.duration >= max(0.5, profile.silence_min_duration * 3):
            if not any(abs(b.time - silence.mid) < 1.0 for b in boundaries):
                weak = Boundary(time=silence.mid, silence=silence, agreement=0.25)
                weak.notes.append("silence without black")
                boundaries.append(weak)

    boundaries.sort(key=lambda b: b.time)
    return _dedupe_close(boundaries)


def _dedupe_close(boundaries: list[Boundary], min_gap: float = 1.0) -> list[Boundary]:
    """Collapse boundaries closer together than a plausible spot."""
    kept: list[Boundary] = []
    for b in boundaries:
        if kept and (b.time - kept[-1].time) < min_gap:
            if b.agreement > kept[-1].agreement:
                kept[-1] = b
            continue
        kept.append(b)
    return kept


def duration_prior(duration: float, tolerance: float = 1.5) -> tuple[float, float | None]:
    """Score a segment length against canonical spot durations.

    Returns (score 0..1, matched canonical duration or None). A 29.9s segment scores ~1.0
    against the 30s slot; a 37s segment scores poorly and drags its confidence down.
    """
    best_score = 0.0
    best_match: float | None = None
    for canonical, weight in CANONICAL_DURATIONS:
        delta = abs(duration - canonical)
        if delta <= tolerance:
            score = 1.0 - (delta / tolerance) * 0.25
        else:
            # Decay smoothly rather than cliffing, so near-misses stay usable.
            score = max(0.0, 0.75 - (delta - tolerance) / 12.0)
        score *= weight
        if score > best_score:
            best_score, best_match = score, canonical
    return best_score, best_match
