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

from brand import GOLD, LILAC, PURPLE

# House colours come from brand.py. They are RGB here; the ASS constants used for on-screen
# overlays are BGR, where the same three bytes mean a different colour entirely.
DIM = (120, 110, 140)

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


def draw_mark(draw, cx: float, cy: float, r: float, *, shell=None, pin=None) -> None:
    """The coax mark — a C-shaped shell with a centre conductor, mouth at twelve o'clock.

    Shared with the app icon so the ident and the icon are unmistakably the same logo. A
    station that uses two different marks reads as two stations.
    """
    shell = shell or (168, 92, 246)
    pin = pin or (255, 200, 60)
    width = max(2, int(r * 0.30))
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=310, end=230,
             fill=shell + (255,), width=width)
    pin_r = r * 0.30
    draw.ellipse([cx - pin_r, cy - pin_r, cx + pin_r, cy + pin_r], fill=pin + (255,))


def _gradient(size: tuple[int, int], top, bottom):
    """A vertical wash. Flat colour is what makes a card look like a terminal."""
    from PIL import Image

    width, height = size
    ramp = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        ramp.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return ramp.resize(size)


def card(
    path: Path,
    *,
    heading: str,
    subheading: str = "",
    footer: str = "",
    size: tuple[int, int] = (1920, 1080),
) -> Path:
    """A network ident, not a status message.

    The difference between this and a terminal splash is entirely in the trimmings: a wash
    rather than flat black, a mark that matches the app, rules that give the type somewhere
    to sit, and generous space. None of it is clever — it is the set of things every station
    on air in 1994 had and a shell script does not.
    """
    from PIL import ImageDraw

    width, height = size
    image = _gradient(size, (34, 18, 68), (9, 7, 14)).convert("RGB")
    draw = ImageDraw.Draw(image)

    band_h = int(height * 0.34)
    band_y = int(height * 0.36)
    draw.rectangle([0, band_y, width, band_y + band_h], fill=(22, 13, 44))
    draw.rectangle([0, band_y - 3, width, band_y], fill=PURPLE)
    draw.rectangle([0, band_y + band_h, width, band_y + band_h + 3], fill=PURPLE)

    mark_r = height * 0.090
    mark_cx = width * 0.200
    mark_cy = band_y + band_h / 2
    draw_mark(draw, mark_cx, mark_cy, mark_r)

    text_x = mark_cx + mark_r * 1.85

    # Measure both lines and centre the pair on the band. Deriving the second line's position
    # from the first one's bounding box is what previously let them overlap: a bbox top is
    # not a line height, so tall glyphs ate the gap.
    head_font = _font(int(height * 0.110))
    sub_font = _font(int(height * 0.042))
    hl, ht, hr, hb = draw.textbbox((0, 0), heading, font=head_font)
    head_h = hb - ht
    gap = height * 0.030
    sub_h = 0
    if subheading:
        sl, st_, sr, sb = draw.textbbox((0, 0), subheading, font=sub_font)
        sub_h = sb - st_

    total_h = head_h + (gap + sub_h if subheading else 0)
    top_y = mark_cy - total_h / 2

    draw.text((text_x, top_y - ht), heading, font=head_font, fill=GOLD)
    if subheading:
        draw.text((text_x + 3, top_y + head_h + gap - st_), subheading,
                  font=sub_font, fill=LILAC)

    if footer:
        _centred(draw, footer, _font(int(height * 0.028)), int(height * 0.885), width, DIM)

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
    frames = int(round(seconds * 30000 / 1001))
    # A slow push-in plus fades at both ends. Costs nothing and is most of the difference
    # between a station ident and a screenshot of one.
    vf = (
        f"scale=2560:1440,zoompan=z='min(zoom+0.00045,1.10)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=30000/1001,"
        f"fade=t=in:st=0:d=0.35,fade=t=out:st={max(0.0, seconds - 0.45):.2f}:d=0.45,"
        "format=yuv420p"
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-loop", "1", "-framerate", "30000/1001", "-i", str(still),
         "-f", "lavfi", "-i", audio,
         "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60",
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
