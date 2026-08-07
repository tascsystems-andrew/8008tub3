"""Per-file analysis: what does this source actually need?

Every decision here is made once and baked into the output permanently, which is why each
one is measured rather than assumed. `ffprobe`'s `field_order` in particular is not
trustworthy on captured material — plenty of VHS captures are muxed as "progressive" while
carrying interlaced fields, so interlacing is detected by counting fields with `idet`
instead of believing the container.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from mediakit.ffmpeg import MediaInfo, run

_IDET_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)"
)
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

# Minimum fraction of a dimension a trim must remove before it counts as a real bar
# rather than a dark edge. A genuine letterbox removes 10-25%.
MIN_CROP_FRACTION = 0.04


@dataclass(frozen=True)
class InterlaceVerdict:
    mode: str        # "progressive" | "tff" | "bff"
    confidence: float
    tff: int = 0
    bff: int = 0
    progressive: int = 0
    undetermined: int = 0

    @property
    def interlaced(self) -> bool:
        return self.mode in ("tff", "bff")


@dataclass(frozen=True)
class CropVerdict:
    width: int
    height: int
    x: int
    y: int
    source_width: int
    source_height: int

    @property
    def trims(self) -> bool:
        return (self.width, self.height) != (self.source_width, self.source_height)

    @property
    def filter_string(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def detect_interlacing(info: MediaInfo, sample_frames: int = 800) -> InterlaceVerdict:
    """Count fields with idet rather than trusting the container's field_order."""
    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats",
         "-i", str(info.path), "-vf", "idet",
         "-frames:v", str(sample_frames), "-an", "-f", "null", "-"],
        capture_stderr=True,
    )
    # ffmpeg emits this summary twice — once when the filter is configured, with all
    # counters at zero, and once at end of stream with the real totals. Taking the first
    # match reports every source as progressive.
    matches = _IDET_RE.findall(stderr)
    if not matches:
        return InterlaceVerdict(mode="progressive", confidence=0.0)

    tff, bff, prog, undet = (int(g) for g in matches[-1])
    total = tff + bff + prog + undet
    if total == 0:
        return InterlaceVerdict(mode="progressive", confidence=0.0)

    # The discriminator is *parity dominance*, not interlaced-vs-progressive counts.
    #
    # Genuinely interlaced material is overwhelmingly one parity: a real TFF capture scores
    # TFF=360 BFF=0. Progressive material with fine detail also trips idet — but it splits
    # roughly evenly between the two parities (TFF=127 BFF=138), because that is noise
    # rather than field order. Counting tff+bff against prog therefore flags detailed
    # progressive footage as interlaced, and deinterlacing progressive material throws away
    # real vertical resolution. When in doubt, leave it alone.
    interlaced = tff + bff
    decided = tff + bff + prog

    if decided == 0:
        # Everything undetermined — a static shot with no motion to judge. Not a finding.
        return InterlaceVerdict("progressive", 0.0, tff, bff, prog, undet)

    dominant = max(tff, bff)
    parity_ratio = dominant / interlaced if interlaced else 0.0
    interlaced_share = interlaced / decided

    if interlaced_share >= 0.6 and parity_ratio >= 0.75:
        mode = "tff" if tff >= bff else "bff"
        # Confidence blends "is it interlaced" with "is it unambiguously one parity".
        confidence = round(min(1.0, interlaced_share * parity_ratio), 3)
        return InterlaceVerdict(mode, confidence, tff, bff, prog, undet)

    return InterlaceVerdict("progressive", round(1.0 - interlaced_share * parity_ratio, 3),
                            tff, bff, prog, undet)


def detect_crop(info: MediaInfo, samples: int = 5) -> CropVerdict:
    """Find the real active picture, ignoring baked-in letterbox/pillarbox bars.

    Sampling matters: cropdetect run over a dark scene will happily report that the picture
    is half its real size. We sample across the running time and keep the *largest* region
    any sample claims, which is the union of everything that was ever not-black.
    """
    if info.duration <= 0 or not info.width:
        return CropVerdict(info.width, info.height, 0, 0, info.width, info.height)

    best_w, best_h, best_x, best_y = 0, 0, 0, 0
    span = info.duration
    points = [span * (i + 1) / (samples + 1) for i in range(samples)]

    for at in points:
        stderr = run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-ss", f"{at:.2f}", "-i", str(info.path),
             "-vf", "cropdetect=limit=24:round=2:reset=0",
             "-frames:v", "120", "-an", "-f", "null", "-"],
            capture_stderr=True,
        )
        matches = _CROP_RE.findall(stderr)
        if not matches:
            continue
        w, h, x, y = (int(v) for v in matches[-1])
        if w * h > best_w * best_h:
            best_w, best_h, best_x, best_y = w, h, x, y

    if best_w <= 0 or best_h <= 0:
        return CropVerdict(info.width, info.height, 0, 0, info.width, info.height)

    # Refuse to crop away more than a third of either dimension. Anything that aggressive is
    # a detection failure, not a letterbox, and cropping it would destroy the picture.
    if best_w < info.width * 0.66 or best_h < info.height * 0.66:
        return CropVerdict(info.width, info.height, 0, 0, info.width, info.height)

    # Ignore trivial trims. Analog captures and VHS rips almost always have a slightly dark
    # edge or a few dead pixels, and cropdetect duly reports 640x480 -> 640x476. Four pixels
    # is not a letterbox — but acting on it forces a full re-encode of a file that could
    # otherwise have been stream-copied, which is both slower and lossy. Only a trim large
    # enough to be a real bar is worth that price: a genuine letterbox removes 10-25% of the
    # height, an order of magnitude more than this threshold.
    trimmed_w = (info.width - best_w) / info.width if info.width else 0.0
    trimmed_h = (info.height - best_h) / info.height if info.height else 0.0
    if trimmed_w < MIN_CROP_FRACTION and trimmed_h < MIN_CROP_FRACTION:
        return CropVerdict(info.width, info.height, 0, 0, info.width, info.height)

    return CropVerdict(best_w, best_h, best_x, best_y, info.width, info.height)


@dataclass(frozen=True)
class LoudnessMeasurement:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    def as_filter_args(self) -> str:
        return (
            f":measured_I={self.input_i}"
            f":measured_TP={self.input_tp}"
            f":measured_LRA={self.input_lra}"
            f":measured_thresh={self.input_thresh}"
            f":offset={self.target_offset}"
        )


def measure_loudness(
    info: MediaInfo, target_lufs: float, true_peak: float, loudness_range: float
) -> LoudnessMeasurement | None:
    """First pass of a two-pass EBU R128 normalisation.

    One-pass loudnorm is a dynamic compressor that changes gain as it goes; two-pass applies
    a single measured offset and preserves the mix. For material where an ad break must not
    audibly jump against the show either side of it, the difference is the whole point.
    """
    if not info.has_audio:
        return None

    stderr = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(info.path),
         "-af", f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={loudness_range}:print_format=json",
         "-vn", "-f", "null", "-"],
        capture_stderr=True,
    )

    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(stderr[start:end + 1])
    except json.JSONDecodeError:
        return None

    def _f(key: str) -> float | None:
        value = data.get(key)
        if value in (None, "", "-inf", "inf"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    fields = [_f(k) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")]
    if any(v is None for v in fields):
        return None
    return LoudnessMeasurement(*fields)  # type: ignore[arg-type]
