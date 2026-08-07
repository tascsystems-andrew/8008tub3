"""Generated station identification — the bookends that make a break read as television.

FieldStation42 wraps every ad pod as start-bump → commercials → end-bump, and it *requires*
a bump pool: an empty `bump_dir` survives the catalog build but raises during schedule build,
because a pod always asks for a bumper.

Rather than treat that as an obstacle, satisfy it properly. A 90s channel without station IDs
isn't a 90s channel — the ident before and after the break is a large part of why a break
reads as broadcast rather than as a playlist.

Crucially these are **generated, not sourced**. The project's firm rule is that it ships zero
content and ingests only user-supplied files; commercials are copyrighted works. Cards we
draw ourselves carry no such problem, so every install gets proper bookends out of the box
with nothing to license.

Drawn with Pillow rather than ffmpeg's `drawtext`, which is a compile-time option and is
absent from plenty of builds — including Homebrew's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Amber on near-black: what the era's idents, BIOS screens and cable boxes converged on, and
# it survives being photographed off a TV better than white on grey.
AMBER = (0, 215, 255)
DARK = (16, 16, 16)
DIM = (90, 90, 90)

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _centred(draw, text: str, font, y: int, width: int, fill) -> int:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, y), text, font=font, fill=fill)
    return bottom - top


def card(
    path: Path,
    *,
    heading: str,
    subheading: str = "",
    footer: str = "",
    size: tuple[int, int] = (1280, 720),
) -> Path:
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", size, DARK)
    draw = ImageDraw.Draw(image)

    # A pair of rules above and below the text, which is the cheapest thing that reads as
    # "broadcast ident" rather than "error message".
    inset = int(width * 0.12)
    draw.rectangle([inset, int(height * 0.30), width - inset, int(height * 0.30) + 4], fill=AMBER)
    draw.rectangle([inset, int(height * 0.68), width - inset, int(height * 0.68) + 4], fill=AMBER)

    y = int(height * 0.36)
    y += _centred(draw, heading, _font(int(height * 0.16)), y, width, AMBER) + int(height * 0.04)
    if subheading:
        _centred(draw, subheading, _font(int(height * 0.06)), y, width, AMBER)
    if footer:
        _centred(draw, footer, _font(int(height * 0.035)), int(height * 0.85), width, DIM)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def card_clip(
    dest: Path,
    *,
    heading: str,
    subheading: str = "",
    seconds: float = 5.0,
    tone_hz: int = 0,
) -> Path:
    """Turn a card into a short clip with silence (or a test tone) under it."""
    if dest.exists() and dest.stat().st_size > 4096:
        return dest

    still = dest.with_suffix(".png")
    card(still, heading=heading, subheading=subheading)

    audio = (
        f"sine=frequency={tone_hz}:sample_rate=48000"
        if tone_hz else
        "anullsrc=sample_rate=48000:channel_layout=stereo"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-loop", "1", "-framerate", "30000/1001", "-i", str(still),
         "-f", "lavfi", "-i", audio,
         "-vf", "scale=1280:720,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-g", "60",
         "-c:a", "aac", "-b:a", "128k",
         "-t", f"{seconds}", str(dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )
    still.unlink(missing_ok=True)
    return dest


def make_station_ids(bump_root: Path, channel: int, name: str) -> dict[str, int]:
    """Populate pre/ and post/ bump pools.

    Upstream splits a bump directory into `<tag>-prebump` and `<tag>-postbump` purely by
    subfolders literally named `pre` and `post` — no config, no naming convention beyond
    those two words.
    """
    made = {"pre": 0, "post": 0}

    pre = [
        ("WE'LL BE", "RIGHT BACK"),
        (f"CHANNEL {channel}", name),
    ]
    post = [
        ("YOU'RE WATCHING", name),
        (f"CHANNEL {channel}", "STAY TUNED"),
    ]

    for index, (heading, subheading) in enumerate(pre):
        card_clip(bump_root / "pre" / f"ident_pre_{index}.mkv",
                  heading=heading, subheading=subheading, seconds=4.0)
        made["pre"] += 1

    for index, (heading, subheading) in enumerate(post):
        card_clip(bump_root / "post" / f"ident_post_{index}.mkv",
                  heading=heading, subheading=subheading, seconds=4.0)
        made["post"] += 1

    return made
