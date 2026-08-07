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


# Purple, ringed, and drifting downward. Reads three ways at once — a CRT phosphor ring, a
# tuning dial, and the joke the project is named after — which is about as much as an icon
# can be asked to do. Original artwork: the purple concentric look belongs to mpv's own logo,
# and shipping that would misrepresent whose software this is.
# Three rings, not five. A dock icon is often 32px, where five thin strokes collapse into
# a purple smudge — fewer, thicker rings survive the shrink and read cleaner large.
RING_COLOURS = (
    (96, 38, 176),
    (146, 64, 232),
    (198, 116, 250),
)
ICON_BG = (14, 12, 20, 255)


def draw_icon(px: int):
    """One icon at a given pixel size. Supersampled, because thin rings alias badly."""
    from PIL import Image, ImageDraw

    ss = 4                      # supersample factor
    size = px * ss
    image = Image.new("RGBA", (size, size), ICON_BG)
    draw = ImageDraw.Draw(image)

    centre_x = size / 2
    outer_r = size * 0.375
    # Each ring is smaller than the last and sits slightly lower, so the whole stack drifts
    # toward the bottom rather than sharing one centre.
    top_y = size * 0.415
    drift = size * 0.155

    rings = len(RING_COLOURS)
    for index, colour in enumerate(RING_COLOURS):
        fraction = index / (rings - 1)
        radius = outer_r * (1.0 - 0.255 * index)
        centre_y = top_y + drift * fraction
        width = max(1, int(size * (0.082 - 0.011 * index)))
        draw.ellipse(
            [centre_x - radius, centre_y - radius, centre_x + radius, centre_y + radius],
            outline=colour + (255,), width=width,
        )

    # The centre, sitting at the bottom of the drift.
    tip_r = outer_r * 0.235
    tip_y = top_y + drift * 1.30
    draw.ellipse([centre_x - tip_r, tip_y - tip_r, centre_x + tip_r, tip_y + tip_r],
                 fill=(232, 158, 255, 255))

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
