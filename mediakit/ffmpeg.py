"""Thin wrappers over ffmpeg/ffprobe.

Everything shells out. No Python media libraries — this has to install cleanly on a
Raspberry Pi and ffmpeg is already a hard dependency of the playout layer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegMissing(RuntimeError):
    pass


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegMissing(f"{tool} not found on PATH")


def run(cmd: list[str], *, capture_stderr: bool = False) -> str:
    """Run a command, returning stdout (or stderr when the filters log there)."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if capture_stderr:
        return proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = "\n".join(detail[-4:]) if detail else "(no stderr)"
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd[:6])}…\n{tail}")
    return proc.stdout.decode("utf-8", errors="replace")


def run_raw(cmd: list[str]) -> bytes:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return proc.stdout


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    interlaced: bool
    chapters: tuple[float, ...]

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def probe(path: Path) -> MediaInfo:
    out = run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ])
    data = json.loads(out or "{}")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _first_float(
        data.get("format", {}).get("duration"),
        (video or {}).get("duration"),
    )

    fps = 0.0
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, _, den = video["avg_frame_rate"].partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    # field_order absent or "progressive" means progressive; anything else is interlaced.
    field_order = (video or {}).get("field_order", "progressive")
    interlaced = field_order not in ("progressive", "unknown", None)

    chapters = tuple(
        _first_float(c.get("start_time")) for c in data.get("chapters", [])
        if c.get("start_time") is not None
    )

    return MediaInfo(
        path=path,
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        fps=fps,
        has_audio=audio is not None,
        video_codec=(video or {}).get("codec_name") or "none",
        interlaced=interlaced,
        chapters=chapters,
    )


def _first_float(*values: object, default: float = 0.0) -> float:
    for value in values:
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return default
