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


def make_icon(dest: Path, tmp: Path) -> Path | None:
    """Draw an icon rather than shipping one: fewer files, and it matches the menu."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    iconset = tmp / "tub3.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    for size in ICON_SIZES:
        for scale in (1, 2):
            px = size * scale
            image = Image.new("RGBA", (px, px), (16, 16, 16, 255))
            draw = ImageDraw.Draw(image)
            # A CRT: rounded screen, amber scanline, chunky bezel.
            pad = px * 0.13
            draw.rounded_rectangle([pad, pad, px - pad, px - pad * 1.25],
                                   radius=px * 0.10, fill=(28, 28, 28, 255),
                                   outline=(0, 215, 255, 255), width=max(1, px // 28))
            inner = px * 0.24
            draw.rounded_rectangle([inner, inner, px - inner, px - inner * 1.35],
                                   radius=px * 0.05, fill=(0, 215, 255, 40))
            band = px * 0.055
            mid = px * 0.47
            draw.rectangle([inner * 1.15, mid - band / 2, px - inner * 1.15, mid + band / 2],
                           fill=(0, 215, 255, 255))
            # Two little rabbit-ear antennae, because of course.
            draw.line([px * 0.42, pad, px * 0.30, px * 0.03], fill=(0, 215, 255, 255),
                      width=max(1, px // 32))
            draw.line([px * 0.58, pad, px * 0.70, px * 0.03], fill=(0, 215, 255, 255),
                      width=max(1, px // 32))

            suffix = f"{size}x{size}" + ("@2x" if scale == 2 else "")
            image.save(iconset / f"icon_{suffix}.png")

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
