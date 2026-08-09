"""Take film and episode lengths from Plex instead of probing for them.

Upstream learns how long a file is by opening it — ffprobe, falling back to moviepy. That is
correct and it is the single most expensive thing a schedule build does: over the site-to-site
VPN it is the difference between a channel appearing in minutes and appearing in an hour. And
it is redundant, because Plex opened every one of these files when it scanned the library and
wrote the answer down.

So this seeds upstream's own cache rather than replacing anything. `file_meta` is consulted
before any probe (`MediaProcessor._process_media` -> `check_file_cache`), so a row that is
already there is a probe that never happens. Nothing upstream is modified, patched or
monkeyed with; a table it owns simply turns out to be populated.

**What this does not save.** Every scan still calls `rich_find_media`, which stats each file
to notice changes — measured at 12 files/second across the VPN, about a minute per channel and
sixteen for the whole dial. That cost is inherent to how upstream discovers media and is not
addressable from here. Seeding must therefore carry a *true* size and mtime: upstream compares
them and re-probes on a mismatch, so a row invented with zeros is worse than no row at all.

The win is the first sight of a file, which is exactly the case that hurts — every time Sonarr
brings a series in, every new channel, every rebuild after a library change.

    python3 -m tub3.durations            # seed everything the pools point at
    python3 -m tub3.durations --dry-run  # say what would be seeded
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

MEDIA = Path(__file__).resolve().parent.parent / "media"
DB = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42" / "runtime" / "fs42_fluid.db"


def plex_durations() -> dict[str, float]:
    """Every duration Plex knows, keyed by file name.

    Keyed by basename rather than full path on purpose. Plex sees the library through its own
    mount — its paths start somewhere else entirely — and the same reconciliation problem is
    already solved this way in `tub3.plex.path_index`. A basename collision across two
    libraries would mean two files with identical names *and* identical lengths mattering to
    the second, which is not a failure worth engineering against.
    """
    from .plex import from_config

    client = from_config()
    if client is None:
        return {}

    out: dict[str, float] = {}
    for item in client.library():
        # Films carry their length on the item; episodes need the series walked.
        if item.kind == "movie":
            for path in item.paths:
                seconds = (item.minutes or 0) * 60.0
                if seconds > 0:
                    out[os.path.basename(path)] = seconds
            continue
        for episode in client.episodes_of(item):
            if episode.path and getattr(episode, "seconds", 0):
                out[os.path.basename(episode.path)] = float(episode.seconds)
    return out


def pool_targets() -> list[str]:
    """Every real file the symlink pools point at.

    `os.readlink`, not `realpath` — reading a symlink is a local operation and resolving one
    is a round trip to the NAS. For eleven thousand files that is the difference between
    instant and a quarter of an hour, and the target string is all that is needed here.
    """
    targets: set[str] = set()
    if not MEDIA.is_dir():
        return []
    for station in MEDIA.iterdir():
        if not station.is_dir():
            continue
        for tag in station.iterdir():
            try:
                names = os.listdir(tag)
            except OSError:
                continue
            for name in names:
                link = tag / name
                try:
                    targets.add(os.readlink(link) if link.is_symlink() else str(link))
                except OSError:
                    continue
    return sorted(targets)


def seed(dry_run: bool = False) -> tuple[int, int, int]:
    """Fill `file_meta` for anything Plex knows and upstream has not seen.

    Returns (already cached, seeded, unknown to Plex).
    """
    if not DB.exists():
        print("  no schedule database yet — run a build first")
        return (0, 0, 0)

    lengths = plex_durations()
    if not lengths:
        print("  Plex is not configured or returned nothing")
        return (0, 0, 0)
    print(f"  Plex knows {len(lengths)} durations")

    targets = pool_targets()
    print(f"  pools point at {len(targets)} files")

    conn = sqlite3.connect(DB)
    cached = {row[0] for row in conn.execute("SELECT path FROM file_meta")}
    print(f"  already cached: {len(cached)}")

    known = seeded = unknown = 0
    now = time.time()
    for target in targets:
        if target in cached:
            known += 1
            continue
        seconds = lengths.get(os.path.basename(target))
        if not seconds:
            unknown += 1
            continue
        try:
            info = os.stat(target)
        except OSError:
            unknown += 1
            continue
        if not dry_run:
            conn.execute(
                "INSERT OR IGNORE INTO file_meta "
                "(path, duration, size, first_added, last_mod, last_checked, last_updated, "
                " meta, media_type) VALUES (?,?,?,?,?,?,?,?,?)",
                (target, float(seconds), info.st_size, now, info.st_mtime, now, now,
                 "", "video"),
            )
        seeded += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return (known, seeded, unknown)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="tub3.durations", description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print()
    known, seeded, unknown = seed(dry_run=args.dry_run)
    verb = "would seed" if args.dry_run else "seeded"
    print(f"\n  {known} already cached · {verb} {seeded} · {unknown} Plex could not name\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
