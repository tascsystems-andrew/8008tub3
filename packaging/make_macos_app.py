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
# Run the copy of mpv inside this bundle, so macOS attributes the video window to
# 8008TUB3 rather than to mpv.
export TUB3_MPV="$(cd "$(dirname "$0")" && pwd)/mpv"
cd "$REPO"
exec "$PYTHON" -u -m tub3.app "$@" >>"$HOME/Library/Logs/8008TUB3.log" 2>&1
"""

ICON_SIZES = (16, 32, 64, 128, 256, 512)


# It is the schematic symbol for a coaxial connector: a C-shaped shell opening to one side
# with a filled centre conductor inside it. Which is to say the joke was already sitting in
# the KiCad standard library, drawn by people who were not making one.
#
# Nothing here needs inventing — draw the symbol, give it the project's purple, and make the
# centre pin gold so the icon shares an accent with the on-screen menu.
SHELL = (150, 68, 236)
SHELL_SOFT = (98, 44, 162)
PIN = (255, 200, 60)
ICON_BG = (14, 12, 20, 255)


def draw_icon(px: int):
    """One icon at a given pixel size. Supersampled — thick arcs still alias at the ends."""
    from PIL import Image, ImageDraw

    ss = 4
    size = px * ss
    image = Image.new("RGBA", (size, size), ICON_BG)
    draw = ImageDraw.Draw(image)

    # Sits low and slightly left, which is what tips the symbol over into the other reading.
    cx, cy = size * 0.500, size * 0.505
    r = size * 0.320
    width = int(size * 0.105)

    # A shadow of the shell, low, for weight.
    draw.arc([cx - r, cy - r + size * 0.038, cx + r, cy + r + size * 0.038],
             start=35, end=145, fill=SHELL_SOFT + (255,), width=int(width * 0.85))

    # The shell, mouth at twelve o'clock. PIL sweeps clockwise from 0 at three o'clock, so
    # starting just past the top and ending just before it puts the gap up there.
    draw.arc([cx - r, cy - r, cx + r, cy + r],
             start=310, end=230, fill=SHELL + (255,), width=width)

    # The centre conductor: solid, and the only warm thing in the mark.
    pin_r = size * 0.098
    draw.ellipse([cx - pin_r, cy - pin_r, cx + pin_r, cy + pin_r], fill=PIN + (255,))

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

    # A symlink keeps the dylib search paths of the original install intact while
    # putting the executable inside our bundle, which is what macOS looks at when
    # deciding whose icon a window belongs to.
    mpv_path = shutil.which("mpv")
    if mpv_path:
        link = macos / "mpv"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(Path(mpv_path).resolve())

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
