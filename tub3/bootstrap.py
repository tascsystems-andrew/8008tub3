"""Point at a folder of shows and a folder of commercials; get a channel.

This is the walking skeleton — the smallest thing that produces a watchable channel with
real ad pods. It writes the station config, arranges the media layout, builds the catalog,
and generates a schedule.

Every non-obvious choice below is a landmine found by reading the upstream source, and each
one fails at a *later* stage than the mistake, which is why they are encoded here rather
than left to the user:

- **`content_dir` must be absolute.** `CatalogEntry.path` inherits it verbatim and
  `ReelCutter` copies it straight into the schedule's `plan_json` — `realpath` never gets
  there. A relative `content_dir` produces a schedule full of paths the tuner cannot resolve.
- **`network_type` must be `standard`.** Loop blocks emit back-to-back content with no reel
  blocks at all, so a loop channel can never have commercials. This is the single decision
  that determines whether the whole point of the project works.
- **All seven weekday keys must exist**, or the schedule reader raises `KeyError` outside any
  handler. `day_templates` plus seven references is the idiom.
- **`bump_dir` must exist *and contain files*.** An empty bump folder is not enough: the
  catalog build tolerates it, but the tag is not persisted when it has no entries, so the
  schedule build reloads the catalog without it and raises `NoFillerContentFound` — every ad
  pod asks for a bumper. We generate station idents to satisfy this, which is what a real
  channel had anyway and ships no licensed content.
- **`break_strategy` must be `standard`**, or break points from chapter markers are discarded.
- **Never emit `autobump` or `schedule_offset`** — both have upstream bugs that produce
  either a crash or a schedule that silently stops advancing forever.
- **`standby_image` and `be_right_back_media` must exist on disk.** They are existence-checked
  at config load, and without `be_right_back_media` leftover gaps are silently zeroed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .cards import card, make_station_ids

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42"

# Folder names inside the media root. These become FS42 "tags" — a tag *is* a directory.
SHOWS_TAG = "shows"
COMMERCIAL_TAG = "commercial"
BUMP_TAG = "bump"

# Never name a content folder any of these. Upstream turns such a path component into a
# silent temporal restriction, so a folder called "December" only ever airs in December.
RESERVED_NAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "q1", "q2", "q3", "q4",
    "morning", "daytime", "prime", "late", "overnight",
    "pre", "post", "next",
}


def _link(target: Path, link: Path) -> None:
    """Point a tag directory at the user's folder rather than copying gigabytes."""
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
        else:
            return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target.resolve(), target_is_directory=True)


def _placeholder_image(path: Path, heading: str, subheading: str = "") -> None:
    if path.exists():
        return
    card(path, heading=heading, subheading=subheading)


def arrange_media(
    media_root: Path, programs: Path, ads: Path, channel: int, name: str
) -> None:
    media_root.mkdir(parents=True, exist_ok=True)
    _link(programs, media_root / SHOWS_TAG)
    _link(ads, media_root / COMMERCIAL_TAG)

    # The bump pool cannot be empty. An empty bump_dir survives the catalog build and then
    # raises NoFillerContentFound during schedule build, because every pod asks for a bumper
    # — and the tag disappears entirely when LiquidSchedule reloads the catalog from the DB,
    # since empty tags are not persisted. Generating station IDs satisfies it and is what a
    # real channel had anyway. Because we draw them, no content is shipped or licensed.
    made = make_station_ids(media_root / BUMP_TAG, channel, name)
    print(f"  idents      {made['pre']} pre, {made['post']} post (generated)")


def build_config(
    *,
    media_root: Path,
    channel: int,
    name: str,
    increment: int,
    break_duration: int,
) -> dict:
    # One tag every hour of every day. A real lineup varies this by daypart; the skeleton
    # deliberately does not, so that anything that goes wrong is the pipeline's fault.
    day = {str(hour): {"tags": SHOWS_TAG} for hour in range(24)}

    conf = {
        "network_name": name,
        "channel_number": channel,
        "network_type": "standard",          # loop channels can never carry commercials
        "content_dir": str(media_root.resolve()),   # must be absolute
        "commercial_dir": COMMERCIAL_TAG,
        "bump_dir": BUMP_TAG,                # required by schedule build even when empty
        "commercial_free": False,
        "break_strategy": "standard",        # anything else discards chapter break points
        "break_duration": break_duration,
        "schedule_increment": increment,
        "standby_image": "runtime/tub3_standby.png",
        "be_right_back_media": "runtime/tub3_brb.png",
        "day_templates": {"daily": day},
        "monday": "daily", "tuesday": "daily", "wednesday": "daily",
        "thursday": "daily", "friday": "daily", "saturday": "daily", "sunday": "daily",
    }
    return {"station_conf": conf}


def check_layout(media_root: Path) -> list[str]:
    problems = []
    for char in "[]?*":
        if char in str(media_root):
            problems.append(
                f"content_dir contains {char!r}; upstream globs this path, so it would "
                f"silently match zero files"
            )
    for child in media_root.iterdir():
        if child.is_dir() and child.name.lower() in RESERVED_NAMES:
            problems.append(
                f"folder {child.name!r} is a reserved scheduling hint upstream — content "
                f"inside it would only air at matching times"
            )
    return problems


def run(args: argparse.Namespace) -> int:
    media_root = args.media_root.resolve()
    name = args.name or f"CHANNEL {args.channel}"
    arrange_media(media_root, args.programs.resolve(), args.ads.resolve(),
                  args.channel, name)

    problems = check_layout(media_root)
    if problems:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        return 1

    station = f"tub3_ch{args.channel}"
    conf = build_config(
        media_root=media_root,
        channel=args.channel,
        name=name,
        increment=args.increment,
        break_duration=args.break_duration,
    )

    conf_path = VENDOR / "confs" / f"{station}.json"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(json.dumps(conf, indent=4))
    print(f"  config      {conf_path}")

    # Everything upstream resolves paths against the process working directory — the schedule
    # DB, confs/, and the config schema all default to relative locations.
    os.chdir(VENDOR)
    # chdir sets where upstream *resolves* its relative paths; it does not make the package
    # importable. Both are needed, and they fail at different points if you only do one.
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))

    _placeholder_image(Path("runtime/tub3_standby.png"), "PLEASE", "STAND BY")
    _placeholder_image(Path("runtime/tub3_brb.png"), "WE WILL", "RETURN")

    from fs42.catalog import ShowCatalog          # noqa: PLC0415 - after chdir, deliberately
    from fs42.liquid_schedule import LiquidSchedule  # noqa: PLC0415
    from fs42.station_manager import StationManager  # noqa: PLC0415

    # StationManager is a borg that loads every config in confs/ during __init__ and calls
    # exit(-1) on a bad one, so it must only ever run in a short-lived, supervised process.
    manager = StationManager()
    station_conf = manager.station_by_channel(args.channel)
    if station_conf is None:
        print("  ! station did not load — config rejected upstream", file=sys.stderr)
        return 1

    # A mutable class attribute upstream means a second rebuild in one process skips every
    # directory walk and misses newly added files.
    ShowCatalog.clear_fluid_cache()
    print("  catalog     building…")
    catalog = ShowCatalog(station_conf, rebuild_catalog=True)

    counts: dict[str, int] = {}
    for tag, entries in (catalog.clip_index or {}).items():
        try:
            counts[tag] = len(entries)
        except TypeError:
            counts[tag] = 1
    for tag, count in sorted(counts.items()):
        print(f"                {tag:<24} {count:>4}")

    if not args.no_dedupe:
        from .adcatalog import install  # noqa: PLC0415 - after chdir and sys.path
        install(media_root / COMMERCIAL_TAG, cooldown_minutes=args.cooldown)
        print(f"  dedupe      on, {args.cooldown} min cooldown across perceptual clusters")

    # Regenerating over an existing range silently doubles the schedule. Upstream writes with
    # INSERT OR REPLACE, but the only unique constraint is the autoincrement id and every
    # index is non-UNIQUE, so the conflict clause can never fire.
    import sqlite3  # noqa: PLC0415
    with sqlite3.connect("runtime/fs42_fluid.db") as conn:
        removed = conn.execute(
            "DELETE FROM liquid_blocks WHERE station = ?", (name,)
        ).rowcount
    if removed > 0:
        print(f"  schedule    cleared {removed} existing block(s)")

    print(f"  schedule    generating {args.days} day(s)…")
    schedule = LiquidSchedule(station_conf)
    schedule.add_days(args.days)
    catalog_used = getattr(schedule, "catalog", None)
    if catalog_used is not None and hasattr(catalog_used, "repeats_prevented"):
        relaxed = sum(catalog_used.relaxations.values())
        print(f"                {catalog_used.repeats_prevented} at full cooldown, "
              f"{relaxed} relaxed, {catalog_used.starved} unconstrained")
    print("  done")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.bootstrap", description=__doc__)
    ap.add_argument("--programs", type=Path, required=True, help="folder of shows")
    ap.add_argument("--ads", type=Path, required=True, help="folder of cut commercials")
    ap.add_argument("--media-root", type=Path, required=True,
                    help="where the FS42 content tree is assembled (symlinks, not copies)")
    ap.add_argument("--channel", type=int, default=3)
    ap.add_argument("--name", type=str, default=None)
    ap.add_argument("--increment", type=int, default=10,
                    help="schedule block minutes; must exceed program length to leave ad room")
    ap.add_argument("--break-duration", type=int, default=120)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--cooldown", type=int, default=45,
                    help="minutes before a spot, or its twin, may air again")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="use upstream ad selection unmodified (for comparison)")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
