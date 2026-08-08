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

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg",
                  ".ts", ".webm", ".wmv"}

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42"

# Folder names inside the media root. These become FS42 "tags" — a tag *is* a directory.
SHOWS_TAG = "shows"
COMMERCIAL_TAG = "commercial"
BUMP_TAG = "bump"

# How many files to probe when deriving block length. Enough for a stable median, few
# enough to finish in seconds on a network share.
SAMPLE_SIZE = 60

# Rating ladder, least to most permissive. A ceiling is cumulative: a channel rated `family`
# draws from kids *and* family spots, not family alone.
#
# Kept to three tiers on purpose. This is a setting a non-technical person has to understand
# from the folder name alone, and "how bad can the ads get on this channel" has about three
# useful answers.
RATING_LADDER = ("kids", "family", "late")

# What a user might plausibly call those folders. Matching is case- and space-insensitive so
# "Kids Commercials" and "kids" both land in the same place.
RATING_ALIASES = {
    "kids": "kids", "kid": "kids", "children": "kids", "child": "kids",
    "tvy": "kids", "tvy7": "kids", "g": "kids", "saturdaymorning": "kids",
    "family": "family", "general": "family", "allages": "family", "pg": "family",
    "tvg": "family", "tvpg": "family", "daytime": "family",
    "late": "late", "all": "late", "adult": "late", "primetime": "late",
    "latenight": "late", "night": "late", "mature": "late", "anything": "late",
    # The landing zone. Unsorted MUST map to the most restrictive rating: anything
    # nobody has looked at yet must never be reachable by a kids channel.
    "unsorted": "late", "inbox": "late", "new": "late", "todo": "late",
    "unrated": "late", "misc": "late",
    "tv14": "late", "tvma": "late", "pg13": "late", "r": "late", "everything": "late",
}


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def classify_rating(folder_name: str) -> str | None:
    """Map a user's folder name onto the ladder, or None if it isn't a rating folder."""
    key = _normalise(folder_name)
    if key in RATING_ALIASES:
        return RATING_ALIASES[key]
    # "Kids Commercials" -> strip the noise word and retry.
    stripped = key.replace("commercials", "").replace("commercial", "").replace("ads", "")
    return RATING_ALIASES.get(stripped)


def pool_tag(rating: str) -> str:
    # The 'ads-' prefix is not cosmetic. Bare 'late' collides with upstream's daypart
    # hint of the same name, and content under it would only ever air late at night.
    return f"ads-{rating}"

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


def build_rating_pools(media_root: Path, ads: Path) -> dict[str, str]:
    """Turn the user's rating folders into cumulative FS42 tags.

    Two problems to solve at once, and one trick solves both.

    A tag *is* a directory upstream, and nesting does not create tags — a file in
    `Commercials/Kids/` still carries the tag `commercial`, so subfolders alone give one
    undifferentiated pool. And a ceiling has to be cumulative: a `family` channel should draw
    from kids spots as well as family ones.

    So each pool is a directory of **symlinks** to every file at or below its rating. Cheap,
    invisible to upstream (it walks with followlinks=True), and — the part that matters —
    `os.path.realpath` collapses the links back to one path per real file. That means the
    cooldown and the perceptual clusters key on the underlying spot, so a commercial present
    in three pools still cannot air three times in a row.

    Returns {rating: tag} for the pools that actually have content.
    """
    found: dict[str, list[Path]] = {}
    for child in sorted(ads.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        rating = classify_rating(child.name)
        if rating:
            found.setdefault(rating, []).append(child)

    if not found:
        return {}

    pools: dict[str, str] = {}
    for index, rating in enumerate(RATING_LADDER):
        # Cumulative: everything at this rating and below.
        sources = [d for r in RATING_LADDER[: index + 1] for d in found.get(r, [])]
        if not sources:
            continue

        tag = pool_tag(rating)
        pool = media_root / tag
        if pool.exists():
            for stale in pool.iterdir():
                if stale.is_symlink():
                    stale.unlink()
        pool.mkdir(parents=True, exist_ok=True)

        linked = 0
        for source in sources:
            for item in sorted(source.rglob("*")):
                if not item.is_file() or item.name.startswith("."):
                    continue
                if item.suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                # Prefix with the rating folder so two spots of the same name in different
                # tiers cannot collide.
                link = pool / f"{_normalise(source.name)}__{item.name}"
                if not link.exists():
                    link.symlink_to(item.resolve())
                linked += 1
        if linked:
            pools[rating] = tag
    return pools


def _pool_folders(media_root: Path, tag: str, folders: list[Path]) -> int:
    """A tag directory of symlinks, one per episode, drawn from several folders.

    Needed because a library is rarely one folder. A viewer picks the five they want on a
    channel and leaves the rest — home video, downloads in progress, the kids' stuff — out
    of it, and pointing the tag at a common parent would sweep all of that in.

    Symlinks rather than copies: nothing is duplicated, one series can appear on several
    channels, and realpath still collapses to a single file so the ad cooldown and the
    dedupe index keep working across channels.
    """
    pool = media_root / tag
    if pool.exists() or pool.is_symlink():
        # It may be a symlink from the single-folder path taken on a previous build.
        if pool.is_symlink():
            pool.unlink()
        else:
            for stale in pool.iterdir():
                if stale.is_symlink():
                    stale.unlink()
    pool.mkdir(parents=True, exist_ok=True)

    linked = 0
    for folder in folders:
        if not folder.exists():
            continue
        # Prefix with the folder name so two series with an S01E01 cannot collide.
        prefix = _normalise(folder.name)[:28]
        for item in sorted(folder.rglob("*")):
            if not item.is_file() or item.name.startswith("."):
                continue
            if item.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            link = pool / f"{prefix}__{item.name}"
            if not link.exists():
                try:
                    link.symlink_to(item.resolve())
                except OSError:
                    continue
            linked += 1
    return linked


def arrange_media(
    media_root: Path, programs: list[Path] | Path, ads: Path, channel: int, name: str
) -> dict[str, str]:
    media_root.mkdir(parents=True, exist_ok=True)

    folders = [programs] if isinstance(programs, Path) else list(programs)
    if len(folders) == 1:
        # One folder stays a plain symlink: no thousands of links to create, and the
        # user's own directory structure shows through unchanged.
        _link(folders[0], media_root / SHOWS_TAG)
    else:
        count = _pool_folders(media_root, SHOWS_TAG, folders)
        print(f"  shows       {count:>4} episodes from {len(folders)} folders")

    pools = build_rating_pools(media_root, ads)
    if pools:
        for rating in RATING_LADDER:
            if rating in pools:
                count = len(list((media_root / pools[rating]).glob("*")))
                print(f"  ads         {rating:<7} {count:>4} spots  (tag {pools[rating]})")
    else:
        # No rating folders — one flat pool, which is the simplest thing that works.
        _link(ads, media_root / COMMERCIAL_TAG)
        print("  ads         one pool (no rating folders found)")

    # The bump pool cannot be empty. An empty bump_dir survives the catalog build and then
    # raises NoFillerContentFound during schedule build, because every pod asks for a bumper
    # — and the tag disappears entirely when LiquidSchedule reloads the catalog from the DB,
    # since empty tags are not persisted. Generating station IDs satisfies it and is what a
    # real channel had anyway. Because we draw them, no content is shipped or licensed.
    made = make_station_ids(media_root / BUMP_TAG, channel, name)
    print(f"  idents      {made['pre']} pre, {made['post']} post (generated)")
    return pools


def build_config(
    *,
    media_root: Path,
    channel: int,
    name: str,
    increment: int,
    break_duration: int,
    commercial_tag: str,
) -> dict:
    # One tag every hour of every day. A real lineup varies this by daypart; the skeleton
    # deliberately does not, so that anything that goes wrong is the pipeline's fault.
    day = {str(hour): {"tags": SHOWS_TAG} for hour in range(24)}

    conf = {
        "network_name": name,
        "channel_number": channel,
        "network_type": "standard",          # loop channels can never carry commercials
        "content_dir": str(media_root.resolve()),   # must be absolute
        "commercial_dir": commercial_tag,
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


# Block lengths a television schedule actually uses, in minutes.
STANDARD_BLOCKS = (10, 15, 30, 60, 90, 120)

# Real 90s broadcast ran roughly 22 minutes of content in a 30-minute slot, so content is
# about three-quarters of a block. Sizing a block much larger than the programme means the
# scheduler fills the difference with commercials — and it does so by cutting the programme
# into ever smaller pieces, which is how a 450s show in a 1800s block becomes twelve
# 37-second fragments separated by ad pods.
CONTENT_SHARE = 0.75


def choose_increment(programs: list[Path] | Path) -> tuple[int, float, int]:
    """Pick the schedule block length from how long the programmes actually are.

    Returns (minutes, median program seconds, program count). This is deliberately derived
    rather than configured: getting it wrong does not fail, it quietly produces a channel
    that is three-quarters advertising, and no non-technical user is going to work out that
    the fix is a block-length setting.
    """
    from mediakit.ffmpeg import probe  # noqa: PLC0415

    folders = [programs] if isinstance(programs, Path) else list(programs)

    files: list[Path] = []
    for folder in folders:
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if path.name.startswith("."):
                continue
            files.append(path)

    if not files:
        return 30, 0.0, 0

    # Sample, do not measure. Every probe opens a file, and over a network share that is a
    # round trip — a three thousand file library on a NAS across a VPN took long enough that
    # the process sat in uninterruptible sleep with zero CPU while a user watched a blank
    # screen. tub3.inventory already worked this out; this is the same lesson.
    #
    # Spread the sample evenly across the sorted list rather than taking the first N: a
    # library sorted by folder would otherwise be judged entirely on whichever series comes
    # first alphabetically, and a channel of half-hour sitcoms whose first folder happens to
    # be a film gets hour-long blocks.
    step = max(1, len(files) // SAMPLE_SIZE)
    sampled = files[::step][:SAMPLE_SIZE]

    durations = []
    for path in sampled:
        try:
            info = probe(path)
        except Exception:  # noqa: BLE001
            continue
        if info.duration > 0:
            durations.append(info.duration)

    if not durations:
        return 30, 0.0, 0

    if not durations:
        return 30, 0.0, len(files)

    durations.sort()
    median = durations[len(durations) // 2]

    # Pick the block whose resulting ad load lands closest to real broadcast, rather than the
    # first block over a threshold. A threshold is brittle at the boundary — a 450.05s
    # programme misses a 600s cutoff by a twentieth of a second and jumps to the next block
    # up, doubling the advertising. Scoring the outcome directly cannot do that.
    candidates = [m for m in STANDARD_BLOCKS if m * 60 >= median]
    if not candidates:
        return STANDARD_BLOCKS[-1], median, len(files)

    target_ad_share = 1.0 - CONTENT_SHARE
    best = min(candidates, key=lambda m: abs((1.0 - median / (m * 60)) - target_ad_share))
    return best, median, len(files)


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
    programs = [folder.resolve() for folder in args.programs]
    missing = [str(f) for f in programs if not f.exists()]
    if missing:
        for folder in missing:
            print(f"  ! no such folder: {folder}", file=sys.stderr)
        return 1
    pools = arrange_media(media_root, programs, args.ads.resolve(), args.channel, name)

    # A ceiling picks the richest pool at or below it. Asking for `family` on a library
    # with only kids spots quietly gets kids rather than failing.
    commercial_tag = COMMERCIAL_TAG
    if pools:
        wanted = RATING_LADDER.index(args.rating)
        for rating in reversed(RATING_LADDER[: wanted + 1]):
            if rating in pools:
                commercial_tag = pools[rating]
                break
        print(f"  rating      ceiling {args.rating!r} -> pool {commercial_tag!r}")

    problems = check_layout(media_root)
    if problems:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        return 1

    increment = args.increment
    if increment is None:
        increment, median, count = choose_increment(programs)
        ad_share = 100 * (1 - median / (increment * 60)) if increment else 0
        print(f"  blocks      {increment} min, derived from {count} programme(s) "
              f"(median {median/60:.1f} min) -> about {ad_share:.0f}% ads")

    station = f"tub3_ch{args.channel}"
    conf = build_config(
        media_root=media_root,
        channel=args.channel,
        name=name,
        increment=increment,
        break_duration=args.break_duration,
        commercial_tag=commercial_tag,
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
        install(media_root / commercial_tag, cooldown_minutes=args.cooldown)
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
    # Repeatable. A library is rarely one folder, and pointing at a common parent to get
    # several of them sweeps in everything else that happens to live there.
    ap.add_argument("--programs", type=Path, required=True, action="append",
                    metavar="DIR", help="folder of shows; repeat for several")
    ap.add_argument("--ads", type=Path, required=True, help="folder of cut commercials")
    ap.add_argument("--media-root", type=Path, required=True,
                    help="where the FS42 content tree is assembled (symlinks, not copies)")
    ap.add_argument("--channel", type=int, default=3)
    ap.add_argument("--name", type=str, default=None)
    ap.add_argument("--increment", type=int, default=None,
                    help="schedule block minutes; derived from programme length if omitted")
    ap.add_argument("--break-duration", type=int, default=120)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--rating", choices=list(RATING_LADDER), default="late",
                    help="ad ceiling for this channel; cumulative, so family includes kids")
    ap.add_argument("--cooldown", type=int, default=45,
                    help="minutes before a spot, or its twin, may air again")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="use upstream ad selection unmodified (for comparison)")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
