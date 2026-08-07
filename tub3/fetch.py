"""Fetch user-supplied commercial footage into the sorting structure.

A thin, opinionated wrapper around yt-dlp. It downloads what you point it at and nothing
else — 8008TUB3 ships no content, bundles no sources, and has no built-in catalogue of
places to get commercials. What to fetch is the operator's decision and the operator's
responsibility; this only handles the mechanics.

Three settings here are not cosmetic:

**Filenames must be glob-safe.** yt-dlp's default template puts the video id in square
brackets — `Some Title [dQw4w9WgXcQ].mp4`. FieldStation42 discovers content with
`glob.glob(f"{tag_dir}/*.{ext}")`, and a `[` in a path makes glob interpret a character
class, so the file silently never appears in the catalogue. No error, no warning, just a
commercial that never airs. `--restrict-filenames` plus an explicit template avoids it.

**H.264 is preferred over VP9 and AV1.** YouTube will happily serve AV1, which neither Pi
can hardware-decode, and a Pi 5 has no H.264 decoder either but manages it in software.
Asking for avc1 up front avoids a re-encode later.

**Everything lands in `Unsorted/` by default.** That folder counts as the most restrictive
rating, so nothing downloaded can reach a kids channel until it has been looked at.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Prefer H.264 + m4a, then any mp4, then whatever exists. normalize can fix anything, but not
# needing to is faster and lossless.
FORMAT = "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/best"

# No brackets, no spaces, no glob metacharacters. The id keeps re-runs idempotent and makes
# duplicates from different searches obvious.
OUTPUT_TEMPLATE = "%(title).70s__%(id)s.%(ext)s"


def available() -> bool:
    return shutil.which("yt-dlp") is not None


def fetch(
    urls: list[str],
    dest: Path,
    *,
    playlists: bool = False,
    archive: Path | None = None,
    dry_run: bool = False,
) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    archive = archive or (dest / ".tub3-fetched.txt")

    cmd = [
        "yt-dlp",
        "--format", FORMAT,
        "--merge-output-format", "mp4",
        "--restrict-filenames",
        "--output", str(dest / OUTPUT_TEMPLATE),
        # Skip anything already fetched, so re-running a link list is free and safe.
        "--download-archive", str(archive),
        "--no-overwrites",
        "--continue",
        "--ignore-errors",
        "--no-playlist" if not playlists else "--yes-playlist",
        "--progress",
    ]
    cmd += urls

    if dry_run:
        print("  " + " ".join(cmd))
        return 0

    before = _count(dest)
    result = subprocess.run(cmd)
    after = _count(dest)
    print(f"\n  {after - before} new file(s) in {dest}")
    return result.returncode


def _count(folder: Path) -> int:
    from .bootstrap import VIDEO_SUFFIXES

    return sum(
        1 for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    )


def fetch_channel(
    handle: str,
    dest_root: Path,
    *,
    limit: int = 30,
    newest_first: bool = True,
    min_minutes: int = 4,
    dry_run: bool = False,
) -> int:
    """Pull a creator's back catalogue into a folder of its own.

    A folder per creator, because a folder IS a tag downstream — so each creator becomes
    programming a channel can schedule, exactly like a series.

    Three limits that matter, because a channel is not a video:

    - **`--limit`.** A prolific creator has hundreds of videos at twenty-plus minutes each,
      which is tens of gigabytes. Defaulting to unlimited would start a download nobody asked
      for. Thirty videos is roughly ten hours, past the point where a channel stops looping
      audibly.
    - **A minimum duration.** Shorts and one-minute clips are not programming; they would be
      scheduled as though they were episodes and leave the block full of filler.
    - **The archive file.** Re-running the same handle fetches only what is new, so this is
      safe to put on a timer later.
    """
    name = handle.lstrip("@").strip("/")
    if "youtube.com" in name or "http" in name:
        # Accept a full URL too, and recover the handle from it for the folder name.
        name = name.rstrip("/").split("/")[-1].lstrip("@")
    dest = dest_root / name
    dest.mkdir(parents=True, exist_ok=True)

    url = handle if handle.startswith("http") else f"https://www.youtube.com/@{name}/videos"

    cmd = [
        "yt-dlp",
        "--format", FORMAT,
        "--merge-output-format", "mp4",
        "--restrict-filenames",
        "--output", str(dest / OUTPUT_TEMPLATE),
        "--download-archive", str(dest / ".tub3-fetched.txt"),
        "--no-overwrites", "--continue", "--ignore-errors",
        "--yes-playlist",
        "--playlist-end", str(limit),
        # Shorts are not episodes. Filtering here rather than after keeps them off the disk.
        "--match-filter", f"duration >= {min_minutes * 60}",
        "--progress",
        url,
    ]
    if not newest_first:
        cmd.insert(-1, "--playlist-reverse")

    if dry_run:
        print("  " + " ".join(cmd))
        return 0

    before = _count(dest)
    subprocess.run(cmd)
    after = _count(dest)
    print(f"\n  {name}: {after - before} new, {after} total in {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.fetch", description=__doc__)
    ap.add_argument("urls", nargs="*", help="one or more URLs, or @handles with --channels")
    ap.add_argument("--channels", action="store_true",
                    help="treat the arguments as creator handles and fetch their catalogues")
    ap.add_argument("--limit", type=int, default=30,
                    help="videos per creator (default 30, roughly ten hours)")
    ap.add_argument("--min-minutes", type=int, default=4,
                    help="skip anything shorter; shorts are not programming")
    ap.add_argument("--into", type=Path, required=True,
                    help="your commercials root (the folder holding Kids/Family/Late/Unsorted)")
    ap.add_argument("--rating", default="Unsorted",
                    help="subfolder to land in; defaults to Unsorted, the safe default")
    ap.add_argument("--from-file", type=Path, help="a file of URLs, one per line")
    ap.add_argument("--playlists", action="store_true", help="follow playlists")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not available():
        print("error: yt-dlp not found on PATH (brew install yt-dlp)", file=sys.stderr)
        return 2

    urls = list(args.urls)
    if args.from_file:
        urls += [
            line.strip() for line in args.from_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if not urls:
        print("error: no URLs given", file=sys.stderr)
        return 2

    if args.channels:
        # Creators land under the target directly, one folder each — they are programming,
        # not commercials, so the rating tiers do not apply.
        print(f"\n  {len(urls)} creator(s) -> {args.into}\n")
        for handle in urls:
            fetch_channel(handle, args.into, limit=args.limit,
                          min_minutes=args.min_minutes, dry_run=args.dry_run)
        return 0

    dest = args.into / args.rating
    print(f"\n  {len(urls)} URL(s) -> {dest}\n")
    return fetch(urls, dest, playlists=args.playlists, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
