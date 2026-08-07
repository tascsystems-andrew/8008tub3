"""What the playback device can actually decode.

This is deliberately *not* an "encode everything to one profile" design. That idea comes from
the ErsatzTV/HLS-Direct architecture, where a segmenter demands every file match or program
boundaries break. Playout here is FieldStation42 direct-playing files through mpv on a local
box, and mpv handles mixed resolutions, codecs and frame rates natively. There is nothing to
be uniform *for*.

So the rule is: **touch as little as possible.** Most files should never have their video
re-encoded at all — a stream copy with corrected audio is lossless, roughly ten times faster,
and leaves a 4K master looking like a 4K master. Video is only re-encoded when the device
genuinely cannot play the source, when it is interlaced, or when it has bars baked in.

The decode facts that drive this, both verified:

- **Pi 5 (BCM2712)** has *no* H.264 hardware decoder and no hardware encoder at all. Its one
  video block does HEVC to 4Kp60. Its A76 cores software-decode 1080p H.264 comfortably at
  20-25% CPU, but 4K H.264 is out of reach.
- **Pi 4 (BCM2711)** keeps hardware H.264 to 1080p *and* hardware HEVC to 4Kp60.

The non-obvious consequence: for content above 1080p, **HEVC is the right target on both
boards**, because both decode it in hardware while neither can software-decode 4K H.264.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UHD = (3840, 2160)
FHD = (1920, 1080)


@dataclass(frozen=True)
class Target:
    """A playback device's real decoding envelope."""

    name: str
    # codec -> largest frame the device plays comfortably
    playable: dict[str, tuple[int, int]]
    # what to re-encode to when the source is not playable, by size
    encode_codec_sd: str      # at or below 1080p
    encode_codec_uhd: str     # above 1080p
    # ceiling: nothing is ever encoded larger than this
    max_frame: tuple[int, int]
    crf_h264: int = 19
    crf_hevc: int = 22
    preset: str = "medium"
    audio_bitrate: str = "192k"
    # EBU R128. Broadcast is -23 LUFS and ATSC A/85 is -24 LKFS, both of which assume a
    # transmission chain that isn't here and land quiet on modern gear. -18 is a common
    # home-media target with headroom to spare. What actually matters is that shows and
    # commercials share one number, so an ad break never jumps.
    target_lufs: float = -18.0
    true_peak: float = -1.5
    loudness_range: float = 11.0
    gop_seconds: float = 2.0

    def plays(self, codec: str, width: int, height: int) -> bool:
        limit = self.playable.get(codec.lower())
        if limit is None:
            return False
        max_w, max_h = limit
        return width <= max_w and height <= max_h


# Reference appliance. HEVC in hardware to 4K; H.264 in software, fine to 1080p.
PI5 = Target(
    name="pi5",
    playable={"hevc": UHD, "h265": UHD, "h264": FHD, "mpeg2video": FHD, "mpeg4": FHD, "vp9": FHD},
    encode_codec_sd="h264",
    encode_codec_uhd="hevc",
    max_frame=UHD,
)

# CRT variant — the Pi 4 kept the 3.5mm composite jack the Pi 5 deleted. Hardware H.264.
PI4 = Target(
    name="pi4",
    playable={"h264": FHD, "hevc": UHD, "h265": UHD, "mpeg2video": FHD, "mpeg4": FHD},
    encode_codec_sd="h264",
    encode_codec_uhd="hevc",
    max_frame=UHD,
)

# A desktop, a NAS, or an x86 mini-PC. Plays essentially anything; only fix what is broken.
ANY = Target(
    name="any",
    playable={
        "h264": UHD, "hevc": UHD, "h265": UHD, "av1": UHD, "vp9": UHD,
        "mpeg2video": UHD, "mpeg4": UHD, "vc1": UHD,
    },
    encode_codec_sd="h264",
    encode_codec_uhd="hevc",
    max_frame=UHD,
)

TARGETS = {t.name: t for t in (PI5, PI4, ANY)}
DEFAULT = PI5


@dataclass
class Plan:
    """What this particular file needs. Empty means audio-only work."""

    reencode_video: bool = False
    reasons: list[str] = field(default_factory=list)
    codec: str | None = None
    scale_to: tuple[int, int] | None = None
    deinterlace_parity: str | None = None
    crop: str | None = None

    @property
    def summary(self) -> str:
        return ", ".join(self.reasons) if self.reasons else "video copied untouched"
