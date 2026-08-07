"""Build a double-clickable 8008TUB3.app.

Deliberately a launcher bundle rather than a frozen binary. The box is Python plus ffmpeg
plus mpv driven over IPC, and freezing it with py2app would bundle mpv into the same
distributable — which, since mpv is GPLv2+, would relicense the whole thing. Keeping the
processes separate keeps 8008TUB3 under MPL-2.0 and keeps the app honest about what it is.

The same reasoning is why this is a direct-download app and not an App Store one: the App
Store's terms add usage restrictions GPLv2 forbids, which is the actual reason VLC left.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCHER = """#!/bin/bash
# 8008TUB3 launcher. Resolves its own repo, then hands off to the tuner.
set -e
REPO="__REPO__"
PYTHON="__PYTHON__"
cd "$REPO"
exec "$PYTHON" -u -m tub3.app "$@" >>"$HOME/Library/Logs/8008TUB3.log" 2>&1
"""

ICON_SIZES = (16, 32, 64, 128, 256, 512)


# One open arc, one small dot. Minimal on purpose: an icon is read at 32px in a dock far
# more often than it is admired at 512, and an open curve keeps its shape when a closed ring
# would fill in. Reads as a tuning dial, a broadcast sweep, and — glancingly — the joke the
# project is named for, without insisting on any of them.
#
# Gold dot rather than purple, so the icon and the on-screen menu share an accent colour.
ARC = (150, 68, 236)
ARC_SOFT = (96, 44, 158)
DOT = (255, 200, 60)
ICON_BG = (14, 12, 20, 255)


def draw_icon(px: int):
    """One icon at a given pixel size. Supersampled — thin arcs alias badly."""
    from PIL import Image, ImageDraw

    ss = 4
    size = px * ss
    image = Image.new("RGBA", (size, size), ICON_BG)
    draw = ImageDraw.Draw(image)

    cx, cy = size / 2, size * 0.475
    r = size * 0.335
    width = int(size * 0.088)
    box = [cx - r, cy - r, cx + r, cy + r]

    # A softer, slightly lower arc underneath gives the shape weight at the bottom without
    # adding a second full ring.
    soft = size * 0.045
    draw.arc([box[0], box[1] + soft, box[2], box[3] + soft],
             start=25, end=155, fill=ARC_SOFT + (255,), width=int(width * 0.8))

    # The main arc leaves a gap at the top right. The break is what stops it reading as a
    # plain circle at small sizes.
    draw.arc(box, start=118, end=48, fill=ARC + (255,), width=width)

    dot_r = size * 0.058
    dot_y = cy + size * 0.028
    draw.ellipse([cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r],
                 fill=DOT + (255,))

    return image.resize((px, px), Image.LANCZOS)


def make_icon(dest: Path, tmp: Path) -> Path | None:
    """Draw the icon rather than shipping one: fewer files, and it is ours."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return None

    iconset = tmp / "tub3.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    for size in ICON_SIZES:
        for scale in (1, 2):
            px = size * scale
            suffix = f"{size}x{size}" + ("@2x" if scale == 2 else "")
            draw_icon(px).save(iconset / f"icon_{suffix}.png")

    icns = tmp / "tub3.icns"
    result = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                            capture_output=True)
    if result.returncode != 0 or not icns.exists():
        return None
    shutil.copy(icns, dest)
    return dest


def build(repo: Path, out_dir: Path, python: str) -> Path:
    app = out_dir / "8008TUB3.app"
    if app.exists():
        shutil.rmtree(app)

    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    launcher = macos / "8008TUB3"
    launcher.write_text(
        LAUNCHER.replace("__REPO__", str(repo.resolve())).replace("__PYTHON__", python)
    )
    launcher.chmod(0o755)

    icon_ok = make_icon(resources / "tub3.icns", out_dir / ".iconbuild")
    shutil.rmtree(out_dir / ".iconbuild", ignore_errors=True)

    info = {
        "CFBundleName": "8008TUB3",
        "CFBundleDisplayName": "8008TUB3",
        "CFBundleIdentifier": "net.tub3.appliance",
        "CFBundleVersion": "0.1",
        "CFBundleShortVersionString": "0.1",
        "CFBundleExecutable": "8008TUB3",
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        # It is a television. It should not sit in the dock switcher behaving like a document
        # editor, and it must be allowed to keep the display awake.
        "LSUIElement": False,
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
    }
    if icon_ok:
        info["CFBundleIconFile"] = "tub3.icns"

    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--out", type=Path, default=Path("/Applications"),
                    help="where to place the bundle")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    for tool in ("mpv", "ffmpeg"):
        if shutil.which(tool) is None:
            print(f"warning: {tool} not on PATH — the app will not start without it")

    app = build(args.repo, args.out, args.python)
    print(f"\n  built {app}")
    print(f"  logs  ~/Library/Logs/8008TUB3.log\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
