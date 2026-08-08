"""The boot splash.

A television does not show you a raspberry, four penguins and a wall of systemd. It shows
you nothing, and then it shows you television. Everything between the power button and the
first frame is a seam, and seams are what make an appliance feel like a computer wearing a
costume.

So the mark is drawn by the same `draw_icon` the macOS bundle uses — one definition of the
logo, not two that drift — composited onto a transparent canvas with the wordmark beneath.
Transparent rather than a filled rectangle: Plymouth centres this on its own background, and
a near-black rectangle sitting on true black shows a visible edge on any screen whose
resolution does not match the canvas. Transparent composites cleanly at 1080p and 4K alike.

Rendered on the Mac and committed, rather than generated on the Pi. Two reasons: the tuner
side deliberately has no Pillow (it gets mpv and the standard library, so a broken build
environment cannot stop the television starting), and a committed PNG means the boot screen
is reviewable in a diff instead of being whatever the box happened to render at install time.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging.make_macos_app import PIN, draw_icon  # noqa: E402

CANVAS = 900
MARK_PX = 520
WORDMARK = "BoobTube"

# Candidates in preference order. The Pi has none of these, which is fine — this runs on the
# Mac. DejaVu is last because it is the fallback that always exists somewhere.
FONTS = (
    "/System/Library/Fonts/Avenir Next.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/opt/homebrew/share/fonts/DejaVuSans.ttf",
)


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def build(dest: Path) -> Path:
    from PIL import Image, ImageDraw

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))

    # Transparent background, not the icon's filled one — otherwise the mark arrives as a
    # near-black square floating on Plymouth's black, with a visible edge.
    mark = draw_icon(MARK_PX, bg=(0, 0, 0, 0))
    canvas.paste(mark, ((CANVAS - MARK_PX) // 2, int(CANVAS * 0.10)), mark)

    draw = ImageDraw.Draw(canvas)
    font = _font(int(CANVAS * 0.098))

    # Letterspacing, drawn a glyph at a time. PIL has no tracking control, and the wordmark
    # looks cramped without it at this size.
    tracking = int(CANVAS * 0.012)
    widths = [draw.textlength(ch, font=font) for ch in WORDMARK]
    total = sum(widths) + tracking * (len(WORDMARK) - 1)
    x = (CANVAS - total) / 2
    # The mark's drawn area ends well above its bounding box — the arc's mouth is at the
    # top and its shadow at the bottom — so the wordmark sits closer than the box implies.
    y = int(CANVAS * 0.70)
    for ch, w in zip(WORDMARK, widths):
        draw.text((x, y), ch, font=font, fill=PIN + (255,))
        x += w + tracking

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="make_splash", description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "assets" / "splash.png")
    args = ap.parse_args(argv)

    out = build(args.out)
    size = out.stat().st_size
    print(f"\n  {out}  ({CANVAS}x{CANVAS}, {size / 1024:.0f} KB)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
