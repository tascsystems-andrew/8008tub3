"""The conditioning pass: touch as little as possible, fix what actually matters.

Most files leave here with their video stream copied bit-for-bit. That is the whole design:
a 4K master stays a 4K master, the pass runs roughly ten times faster than a re-encode, and
the only thing that changed is the thing that needed to change.

Video is re-encoded only when there is a real reason:
  - the device cannot decode the source (codec or frame size beyond its envelope),
  - the source is genuinely interlaced,
  - the source has letterbox/pillarbox bars baked into the picture.

Audio is always re-encoded, because loudness matching is the point. Two files 20 dB apart
must come out matched, or an ad break blasts the room and the appliance gets switched off.

Idempotent and resumable: output is keyed on source size, mtime and target, and writes go to
a `.part` file that is atomically renamed. A run killed at 3am resumes exactly where it was.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from mediakit.ffmpeg import MediaInfo, probe, run
from . import chapters as chapters_mod
from .analyze import detect_crop, detect_interlacing, measure_loudness
from .profile import Plan, Target

SIDECAR_SUFFIX = ".tub3.json"

# Act breaks only belong in programs. A commercial does not have a second act.
PROGRAM_MIN_DURATION = 300.0


@dataclass
class Result:
    source: str
    output: str | None
    target: str
    skipped: bool = False
    reason: str | None = None
    video_action: str = "copy"
    plan_reasons: list[str] = field(default_factory=list)
    interlacing: str | None = None
    interlace_confidence: float = 0.0
    cropped: str | None = None
    scaled_to: str | None = None
    loudness_in: float | None = None
    loudness_out_target: float | None = None
    chapters: list[dict] = field(default_factory=list)
    encode_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _identity(info: MediaInfo, target: Target) -> dict:
    stat = info.path.stat()
    return {
        "schema": "tub3.normalized/2",
        "source_size": stat.st_size,
        "source_mtime": int(stat.st_mtime),
        "target": target.name,
    }


def is_current(source: Path, dest: Path, target: Target) -> bool:
    sidecar = dest.with_suffix(dest.suffix + SIDECAR_SUFFIX)
    if not dest.exists() or not sidecar.exists():
        return False
    try:
        recorded = json.loads(sidecar.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    stat = source.stat()
    return (
        recorded.get("source_size") == stat.st_size
        and recorded.get("source_mtime") == int(stat.st_mtime)
        and recorded.get("target") == target.name
    )


def make_plan(info: MediaInfo, target: Target) -> Plan:
    """Decide what this file needs. The default answer is 'nothing'."""
    plan = Plan()

    verdict = detect_interlacing(info)
    if verdict.interlaced:
        plan.reencode_video = True
        plan.deinterlace_parity = verdict.mode
        plan.reasons.append(f"interlaced ({verdict.mode}, {verdict.confidence:.2f})")

    crop = detect_crop(info)
    if crop.trims:
        plan.reencode_video = True
        plan.crop = crop.filter_string
        plan.reasons.append(f"bars cropped to {crop.width}x{crop.height}")
        width, height = crop.width, crop.height
    else:
        width, height = info.width, info.height

    if not target.plays(info.video_codec, width, height):
        plan.reencode_video = True
        limit_w, limit_h = target.max_frame

        # Pick the codec the device actually decodes at this size. Above 1080p that means
        # HEVC on both Pis — neither can software-decode 4K H.264, but both have a hardware
        # HEVC block.
        big = height > 1080 or width > 1920
        plan.codec = target.encode_codec_uhd if big else target.encode_codec_sd

        # Only shrink if the source exceeds the device ceiling. Never upscale.
        if width > limit_w or height > limit_h:
            plan.scale_to = (limit_w, limit_h)
            plan.reasons.append(f"{width}x{height} exceeds {limit_w}x{limit_h}")
        else:
            plan.reasons.append(f"{info.video_codec} not decodable at {width}x{height}")

    if plan.reencode_video and plan.codec is None:
        # Re-encoding for deinterlace or crop: keep the source's own codec family where the
        # device can play it, so nothing is gratuitously converted.
        big = height > 1080 or width > 1920
        plan.codec = target.encode_codec_uhd if big else target.encode_codec_sd

    return plan


def _video_filters(plan: Plan) -> str | None:
    chain: list[str] = []
    if plan.deinterlace_parity:
        # bwdif over yadif — better interpolation for little extra cost. Neither Pi has a
        # hardware deinterlacer, so this is CPU regardless; spend it once, here, offline.
        parity = "tff" if plan.deinterlace_parity == "tff" else "bff"
        chain.append(f"bwdif=mode=send_frame:parity={parity}:deint=all")
    if plan.crop:
        chain.append(plan.crop)
    if plan.scale_to:
        w, h = plan.scale_to
        chain.append(
            f"scale=w='min({w},iw)':h='min({h},ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
        )
    if not chain:
        return None
    chain.append("setsar=1")
    return ",".join(chain)


def normalize_one(
    source: Path,
    dest: Path,
    target: Target,
    *,
    inject_chapters: bool = True,
    force: bool = False,
) -> Result:
    result = Result(source=str(source), output=None, target=target.name)

    if not force and is_current(source, dest, target):
        result.skipped = True
        result.reason = "already current"
        result.output = str(dest)
        return result

    info = probe(source)
    if info.duration <= 0:
        result.reason = "no readable duration"
        return result

    started = datetime.now(timezone.utc)

    plan = make_plan(info, target)
    result.plan_reasons = list(plan.reasons)
    result.video_action = "re-encode" if plan.reencode_video else "copy"
    result.cropped = plan.crop
    result.interlacing = plan.deinterlace_parity or "progressive"
    if plan.scale_to:
        result.scaled_to = f"{plan.scale_to[0]}x{plan.scale_to[1]}"

    audio_filter = None
    if info.has_audio:
        measurement = measure_loudness(
            info, target.target_lufs, target.true_peak, target.loudness_range
        )
        if measurement is not None:
            result.loudness_in = measurement.input_i
            result.loudness_out_target = target.target_lufs
            audio_filter = (
                f"loudnorm=I={target.target_lufs}:TP={target.true_peak}"
                f":LRA={target.loudness_range}{measurement.as_filter_args()}"
                f":linear=true"
            )
        else:
            result.warnings.append("loudness measurement failed; audio left at source level")
    else:
        result.warnings.append("source has no audio track")

    # Act breaks belong in programs, not commercials. Length is the discriminator, same as
    # the ingest pipeline uses.
    marks: list[chapters_mod.Chapter] = []
    if inject_chapters and info.duration >= PROGRAM_MIN_DURATION:
        marks = chapters_mod.find_act_breaks(info)
        result.chapters = [asdict(c) for c in marks]

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")

    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]

    metadata_file = None
    if marks:
        metadata_file = Path(tempfile.mkstemp(suffix=".ffmeta")[1])
        metadata_file.write_text(chapters_mod.to_ffmetadata(marks, info.duration))
        cmd += ["-i", str(metadata_file), "-map_metadata", "1"]

    cmd += ["-map", "0:v:0"]
    if info.has_audio:
        cmd += ["-map", "0:a:0"]

    if plan.reencode_video:
        filters = _video_filters(plan)
        if filters:
            cmd += ["-vf", filters]
        fps = info.fps or 30.0
        gop = max(1, int(round(fps * target.gop_seconds)))
        if plan.codec == "hevc":
            cmd += ["-c:v", "libx265", "-preset", target.preset, "-crf", str(target.crf_hevc),
                    "-tag:v", "hvc1"]
        else:
            cmd += ["-c:v", "libx264", "-preset", target.preset, "-crf", str(target.crf_h264),
                    "-profile:v", "high"]
        cmd += ["-g", str(gop), "-keyint_min", str(gop), "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]

    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", target.audio_bitrate, "-ar", "48000", "-ac", "2"]
        if audio_filter:
            cmd += ["-af", audio_filter]

    # Put the seek index at the FRONT of the file.
    #
    # Muxers default to writing it at the end, and a player cannot present a single frame
    # until it has read it — so opening any file means fetching data from the far end first.
    # On local disk that is invisible. Measured over SMB across a Starlink link it was the
    # difference between a fast open and a 7-second one: a 3.6MB index at the 98% mark took
    # ~2.6s to read before decoding could even start.
    #
    # We control our own output, so there is no reason to inherit that. Costs a rewrite pass
    # at encode time and nothing at all afterwards.
    if dest.suffix.lower() == ".mkv":
        cmd += ["-cues_to_front", "1"]
    elif dest.suffix.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]

    cmd.append(str(tmp))

    try:
        run(cmd)
    except RuntimeError as exc:
        tmp.unlink(missing_ok=True)
        result.reason = f"encode failed: {exc}"
        return result
    finally:
        if metadata_file:
            metadata_file.unlink(missing_ok=True)

    if not tmp.exists() or tmp.stat().st_size < 4096:
        tmp.unlink(missing_ok=True)
        result.reason = "encode produced an empty file"
        return result

    tmp.replace(dest)
    result.output = str(dest)
    result.encode_seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 1)

    sidecar = dest.with_suffix(dest.suffix + SIDECAR_SUFFIX)
    record = _identity(info, target)
    record.update({
        "source_path": str(source),
        "encoded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "encode_seconds": result.encode_seconds,
        "video_action": result.video_action,
        "plan": result.plan_reasons,
        "interlacing": result.interlacing,
        "crop": result.cropped,
        "scaled_to": result.scaled_to,
        "loudness_in_lufs": result.loudness_in,
        "loudness_target_lufs": target.target_lufs,
        "chapters": result.chapters,
        "warnings": result.warnings,
    })
    sidecar.write_text(json.dumps(record, indent=2))
    return result
