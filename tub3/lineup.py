"""A whole dial, not one channel.

`bootstrap` builds a single channel whose every hour draws from the same folder. That is
enough to prove a pipeline and nothing like television. A real lineup has several channels,
and each one changes through the day: cartoons after school, sitcoms at dinner, something
else entirely at eleven.

This compiles a lineup description into one FieldStation42 station config per channel, plus
the tag directories those configs point at.

Two ideas do the work.

**A tag is a directory, so a "genre" is a folder of symlinks.** Upstream discovers content by
globbing a directory, and nesting does not create tags. So a channel that wants sitcoms at
dinner needs a `shows-sitcoms` directory containing links to each series. Links rather than
copies: nothing is duplicated, one series can appear on several channels, and `realpath`
still collapses to a single file so the ad cooldown and dedupe keep working across channels.

**A daypart is just which tag an hour points at.** FieldStation42 models a day as 24 hourly
slots, each naming a tag, with weekday keys referencing named templates. Dayparts here are
written as hour ranges because that is how a human thinks about a schedule, and expanded to
the 24 slots upstream requires.

The lineup file is JSON, not YAML — no dependency, and nobody is meant to hand-edit it
anyway. It is what the planner writes and the web UI edits.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .bootstrap import (
    BUMP_TAG,
    RESERVED_NAMES,
    VENDOR,
    VIDEO_SUFFIXES,
    RATING_LADDER,
    _normalise,
    pool_tag,
)


# The guide lives on channel 2, and channel 1 is left unpopulated. Andrew's instruction,
# and the reason is the dial he actually remembers: Vancouver cable had no channel 1, so a
# box trying to feel like that dial should not invent one.
#
# Reserved as a constant rather than a convention because the dial is *proposed* by a model
# reading a manifest, and "2 is spoken for, 1 does not exist" is exactly the sort of thing
# that gets forgotten when the interesting question is which sitcoms belong at dinner. Same
# shape as the rating audit: the judgement is delegated, the invariant is not.
GUIDE_CHANNEL = 2
UNUSED_CHANNEL = 1

# The hours a child might plausibly be watching alone, on a channel rated for them. Inside
# this window a kids/family channel may carry only content rated for children.
#
# It starts at 6 rather than at midnight deliberately. The overnight slot wrapping past
# midnight — 23:00 to 06:00 — would otherwise land in the guarded window and refuse the
# ordinary late-night schedule every broadcaster has ever run. Nobody is protected by
# treating 3am as children's television, and pretending otherwise costs the whole dial.
CHILDRENS_HOURS_START = 6
CHILDRENS_HOURS_END = 17


@dataclass
class Daypart:
    """Which tag airs during which hours. `hours` is inclusive-start, exclusive-end."""

    start: int
    end: int
    tag: str
    # Block length and break policy for this daypart alone, overriding the channel's.
    #
    # A channel is normally one shape all day — half-hours for a sitcom strip, two hours for
    # films — which is why `schedule_increment` is a station-level setting upstream. An anime
    # channel is not: it runs 22-minute episodes through the afternoon and 120-minute films at
    # night, and a single increment gets one of the two wrong. Half-hours chop a film's tail
    # into the next block; two-hour blocks pad four episodes' worth of dead air after each
    # cartoon.
    #
    # Upstream already supports this through `tag_overrides`, keyed by tag string
    # (liquid_schedule.py:218). Since a daypart *is* a tag here, the override is written
    # where a person reads the schedule rather than in a second parallel structure.
    increment: int | None = None
    breaks: str | None = None

    def hours(self) -> list[int]:
        # Wrap past midnight: 22-02 means 22, 23, 0, 1.
        if self.end > self.start:
            return list(range(self.start, self.end))
        return list(range(self.start, 24)) + list(range(0, self.end))


@dataclass
class Channel:
    number: int
    name: str
    rating: str = "late"
    # "standard" — scheduled content, the only kind this module can build.
    # "guide"    — the scrolling guide on GUIDE_CHANNEL, rendered by guidecast, not by us.
    kind: str = "standard"
    # tag -> the folders whose episodes belong to it
    sources: dict[str, list[str]] = field(default_factory=dict)
    # tag -> case-insensitive substrings; anything matching is kept out of that pool.
    #
    # A library folder is a filing decision, not a rating. "Kids Movies" holds Song of the
    # South and Princess Mononoke alongside Toy Story, because that is where a person filing
    # animation puts them — and the folder rule in `manifest.classify` can only read the
    # folder, so it calls the whole directory `kids` and the two of them inherit it.
    #
    # Plex is meant to be the second opinion that catches exactly this, and when it answers
    # it does. But it is optional by design: the audit still has to refuse on a box where
    # Plex is unreachable, and "unreachable" includes the quieter case of a path index that
    # matched nothing. An exclusion list is the part that does not depend on a network.
    #
    # Substrings rather than globs on purpose. These are written by a person looking at a
    # directory listing, and a glob that silently matches nothing is the same failure this
    # is here to prevent — so `check_sources` reports any pattern that matched no file.
    exclude: dict[str, list[str]] = field(default_factory=dict)
    dayparts: list[Daypart] = field(default_factory=list)
    # Movie channels want long blocks; a sitcom strip wants half-hours.
    increment: int | None = None
    break_duration: int = 120
    # Where breaks go inside a programme.
    #   "standard"  mid-rolls at chapter markers, falling back to proportional positions
    #   "end"       between programmes only, never inside one
    #   "center"    a single mid-point break
    #
    # "end" is the correct setting for anything cut without commercials in the first place.
    # BBC material — Palin, Simon Reeve, Great British Railway Journeys, Long Way Down — has
    # no act breaks anywhere in it, and neither does YouTube creator content. Timer-based
    # insertion into either cuts mid-sentence, which is the single most immersion-breaking
    # thing this system can do.
    breaks: str = "standard"
    # Some channels should carry no advertising at all.
    commercial_free: bool = False

    @property
    def station(self) -> str:
        return f"tub3_ch{self.number}"

    def default_tag(self) -> str:
        if self.dayparts:
            return self.dayparts[0].tag
        return next(iter(self.sources), "shows")


def load(path: Path) -> list[Channel]:
    data = json.loads(Path(path).read_text())
    channels: list[Channel] = []
    for raw in data.get("channels", []):
        dayparts = []
        for part in raw.get("dayparts", []):
            span = str(part["hours"])
            start, _, end = span.partition("-")
            dayparts.append(Daypart(
                int(start), int(end), part["tag"],
                increment=part.get("increment"),
                breaks=part.get("breaks"),
            ))
        channels.append(Channel(
            number=int(raw["number"]),
            name=raw["name"],
            rating=raw.get("rating", "late"),
            kind=raw.get("kind", "standard"),
            sources={k: list(v) for k, v in raw.get("sources", {}).items()},
            exclude={k: list(v) for k, v in raw.get("exclude", {}).items()},
            dayparts=dayparts,
            increment=raw.get("increment"),
            break_duration=int(raw.get("break_duration", 120)),
            breaks=raw.get("breaks", "standard"),
            commercial_free=bool(raw.get("commercial_free", False)),
        ))
    return sorted(channels, key=lambda c: c.number)


class UnsafeLineup(ValueError):
    """A proposed lineup would put adult content where children can reach it."""


class ReservedChannel(ValueError):
    """A proposed lineup claims a channel number that is spoken for."""


class MissingSource(ValueError):
    """A proposed lineup names content that is not on disk."""


def check_reserved(channels: list[Channel]) -> list[str]:
    """Refuse a lineup that puts anything but the guide on `GUIDE_CHANNEL`.

    Separate from `audit` on purpose. That one is a safety rule and raising it means content
    was about to reach a child; this one is a numbering rule and means the dial is wrong.
    Collapsing them would make `UnsafeLineup` mean two different things, and the day it fires
    you want to know which of the two happened without reading the message.

    Returns human-readable problems; empty means the numbering is fine.
    """
    problems: list[str] = []
    for channel in channels:
        if channel.number == GUIDE_CHANNEL and channel.kind != "guide":
            problems.append(
                f"channel {GUIDE_CHANNEL} is reserved for the scrolling guide, but "
                f"{channel.name!r} claims it — give that channel another number"
            )
        if channel.kind == "guide" and channel.number != GUIDE_CHANNEL:
            problems.append(
                f"the guide is channel {GUIDE_CHANNEL}, but {channel.name!r} is a guide "
                f"channel on {channel.number} — move it to {GUIDE_CHANNEL}"
            )
    return problems


def check_sources(channels: list[Channel]) -> list[str]:
    """Refuse a lineup whose sources do not resolve to content.

    `_link_pool` skipped a path that did not exist, on the reasoning that a NAS might be
    half-mounted and a missing series should not stop the whole dial being built. The cost
    of that kindness was silence: `lineup.json` asked for `Kids TV/Pokémon`, the folder on
    disk is `Kids TV/Pokemon` with a plain `e`, and the accent alone quietly removed 207
    episodes from channel 3's afterschool block. The build reported success and the channel
    simply had less on it. Nobody can debug that.

    So a source that resolves to nothing is now a lineup error, checked before anything is
    written, in the same place as the safety audit. A genuinely absent NAS fails every
    source at once and says so, which is the honest outcome — better than a dial that builds
    and is quietly empty.

    Exclusion patterns are checked the same way, for the same reason: a pattern that matches
    nothing means the thing it was written to keep out is still in the pool.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for channel in channels:
        if channel.kind == "guide":
            continue
        for tag, folders in channel.sources.items():
            patterns = [p.lower() for p in channel.exclude.get(tag, [])]
            matched: set[str] = set()
            for folder in folders:
                key = f"{tag}\x00{folder}"
                source = Path(folder)
                if not source.exists():
                    if key not in seen:
                        problems.append(
                            f"channel {channel.number} ({channel.name!r}) tag {tag!r} names "
                            f"{folder!r}, which does not exist — check the spelling, "
                            f"accents included"
                        )
                        seen.add(key)
                    continue
                # Without exclusions the only question is "is anything there", which stops
                # at the first file. A full listing is only needed to prove the patterns
                # match something, so only the channels that use them pay for the walk.
                if not patterns:
                    if not _has_video(source) and key not in seen:
                        problems.append(
                            f"channel {channel.number} ({channel.name!r}) tag {tag!r} names "
                            f"{folder!r}, which holds no video files"
                        )
                        seen.add(key)
                    continue
                items = _sources_in(source)
                if not items:
                    if key not in seen:
                        problems.append(
                            f"channel {channel.number} ({channel.name!r}) tag {tag!r} names "
                            f"{folder!r}, which holds no video files"
                        )
                        seen.add(key)
                    continue
                for item in items:
                    relative = str(item.relative_to(source.parent)).lower()
                    for pattern in patterns:
                        if pattern in relative:
                            matched.add(pattern)
            for pattern in patterns:
                if pattern not in matched:
                    problems.append(
                        f"channel {channel.number} ({channel.name!r}) excludes {pattern!r} "
                        f"from tag {tag!r}, but nothing in that tag matches it — the title "
                        f"may have been renamed, and it is not being kept out"
                    )
    return problems


def audit(channels: list[Channel]) -> list[str]:
    """Refuse a lineup that puts unrated content on a channel rated for children.

    This exists because the dial is meant to be *proposed* by a model reading a manifest,
    and a model can be wrong. Taste is a good thing to delegate; this is not. So the
    creative decision — which shows belong together, what the channel is called, where the
    dinner strip sits — is delegated, and this one rule is not:

        a channel rated `kids` or `family` may only draw from sources the manifest rated
        `kids`, and the check is by path.

    Deliberately not a warning. A warning is something that scrolls past at eleven at night
    while you are debugging something else, and the whole point is that this failure mode
    must be impossible rather than unlikely. `apply` will not write a config until this
    returns empty.

    Returns human-readable problems; empty means safe.
    """
    from .manifest import classify

    # Plex has already matched the library against the standard databases and knows the
    # broadcast rating, which beats any guess made from a folder name. Optional: the audit
    # must still work, and still refuse, on a box where Plex is not configured.
    plex_index: dict = {}
    try:
        from .plex import from_config, path_index
        client = from_config()
        if client is not None:
            plex_index = path_index(client.library())
    except Exception:  # noqa: BLE001 - Plex being unreachable must not disable the guard
        plex_index = {}

    problems: list[str] = []
    for channel in channels:
        if channel.rating not in ("kids", "family"):
            continue

        # Which tags air in children's hours. A general-audience channel — cartoons in the
        # morning, sitcoms at dinner — is normal broadcast, and refusing it wholesale because
        # one evening daypart carries an adult show protects nothing while blocking the dial
        # everyone actually wants. What matters is the *hours*: nothing adult before
        # CHILDRENS_HOURS_END, anything after.
        #
        # The channel's rating still governs the commercial pool for the whole day, because
        # upstream has one pool per station. That is why a channel like this is rated family
        # rather than late: it means no late-night advertising can reach the 9am block, which
        # is the part no daypart rule could fix afterwards.
        guarded = set()
        if channel.dayparts:
            for part in channel.dayparts:
                if any(CHILDRENS_HOURS_START <= hour < CHILDRENS_HOURS_END
                       for hour in part.hours()):
                    guarded.add(part.tag)
        else:
            guarded = set(channel.sources)

        for tag, folders in channel.sources.items():
            if tag not in guarded:
                continue
            for folder in folders:
                path = Path(folder)
                rating, why = classify(path.parent.name, path.name, path)

                from .plex import lookup as plex_lookup
                item = plex_lookup(plex_index, path) if plex_index else None
                if item is not None:
                    from .plex import classify_rating
                    plex_rating, plex_why = classify_rating(item.content_rating)
                    # Take the stricter of the two, never the more permissive. Where they
                    # disagree, being conservative costs a cartoon; being wrong costs the
                    # one thing this check exists to prevent.
                    order = {"kids": 0, "family": 1, "adult": 2, "late": 2}
                    if order.get(plex_rating, 2) >= order.get(rating, 2):
                        rating, why = plex_rating, plex_why

                if rating != "kids":
                    problems.append(
                        f"channel {channel.number} ({channel.name!r}) is rated "
                        f"{channel.rating!r} but tag {tag!r} draws from {folder!r}, "
                        f"which is rated {rating} — {why}"
                    )
    return problems


def _iter_videos(folder: Path):
    """Every video file a source path contributes, whether it names a folder or one file.

    A series is a directory and a film is very often a bare file sitting in the library
    root — `Spirited Away (2001).mkv` next to `Howls.Moving.Castle/`. Globbing only handled
    the first, and `Path("film.mkv").rglob("*")` yields nothing at all, so naming a film
    directly produced an empty pool and no complaint.

    Lazy, so a caller that only needs to know whether *anything* is there can stop after the
    first hit. These walks are over SMB, and `Movies` is several thousand entries.
    """
    if folder.is_file():
        if folder.suffix.lower() in VIDEO_SUFFIXES:
            yield folder
        return
    for item in sorted(folder.rglob("*")):
        if item.is_file() and not item.name.startswith(".") \
                and item.suffix.lower() in VIDEO_SUFFIXES:
            yield item


def _sources_in(folder: Path) -> list[Path]:
    return list(_iter_videos(folder))


def _has_video(folder: Path) -> bool:
    return next(_iter_videos(folder), None) is not None


def _link_pool(media_root: Path, tag: str, folders: list[str],
               exclude: list[str] | None = None) -> int:
    """Build a tag directory of symlinks to every episode in the given folders."""
    if tag.lower() in RESERVED_NAMES:
        raise ValueError(
            f"tag {tag!r} collides with an upstream scheduling hint — content under it "
            f"would only ever air at matching times"
        )

    pool = media_root / tag
    if pool.exists():
        for stale in pool.iterdir():
            if stale.is_symlink():
                stale.unlink()
    pool.mkdir(parents=True, exist_ok=True)

    patterns = [pattern.lower() for pattern in (exclude or [])]

    linked = 0
    for folder in folders:
        source = Path(folder)
        if not source.exists():
            continue
        # A bare file is its own prefix; a directory names the series inside it.
        prefix = _normalise(source.stem if source.is_file() else source.name)[:28]
        for item in _sources_in(source):
            # Match against the path below the source, so a pattern can name either the
            # film ("Song of the South") or a directory holding it.
            relative = str(item.relative_to(source.parent)).lower()
            if any(pattern in relative for pattern in patterns):
                continue
            # Prefix with the series name so two shows with an "S01E01" cannot collide.
            link = pool / f"{prefix}__{item.name}"
            if not link.exists():
                try:
                    link.symlink_to(item.resolve())
                except OSError:
                    continue
            linked += 1
    return linked


def _day_template(channel: Channel) -> dict[str, dict]:
    """Expand hour ranges into the 24 hourly slots upstream requires.

    Any hour a lineup does not mention falls back to the channel's first daypart rather than
    being left empty — an empty hour is dead air, and a viewer reads dead air as a fault.
    """
    fallback = channel.default_tag()
    slots = {str(hour): {"tags": fallback} for hour in range(24)}
    for part in channel.dayparts:
        for hour in part.hours():
            slots[str(hour)] = {"tags": part.tag}
    return slots


def compile_station(channel: Channel, media_root: Path, *, pools: dict[str, str]) -> dict:
    """One FieldStation42 station config."""
    if channel.kind == "guide":
        # Upstream *has* a `network_type: "guide"`, and it is a trap here: it is a Tkinter
        # app (`fs42/guide_tk.py`, `GuideApp(tk.Tk)`) driven from upstream's own player,
        # which we do not run. Tk needs a display server; this box boots to mpv on DRM with
        # no X at all, so emitting that config would produce a channel that cannot draw.
        # The guide is guidecast's job — headless Chromium to HLS, per the build spec.
        raise ValueError(
            f"channel {channel.number} is the scrolling guide; it is rendered by guidecast "
            f"and has no station config. Upstream's network_type 'guide' is Tk-based and "
            f"will not run on a DRM-only box."
        )
    commercial_tag = pools.get(channel.rating)
    if commercial_tag is None:
        # Fall back down the ladder: asking for family on a kids-only library gets kids.
        wanted = RATING_LADDER.index(channel.rating)
        for rating in reversed(RATING_LADDER[: wanted + 1]):
            if rating in pools:
                commercial_tag = pools[rating]
                break
    if channel.commercial_free:
        commercial_tag = None

    conf = {
        "network_name": channel.name,
        "channel_number": channel.number,
        "network_type": "standard",
        "content_dir": str(media_root.resolve()),
        "bump_dir": BUMP_TAG,
        "commercial_free": commercial_tag is None,
        # Chapter-derived break points are discarded by anything other than "standard", so a
        # channel asking for "end" is explicitly saying its content has no act structure
        # worth finding.
        "break_strategy": channel.breaks,
        "break_duration": channel.break_duration,
        "schedule_increment": channel.increment or 30,
        "standby_image": "runtime/tub3_standby.png",
        "be_right_back_media": "runtime/tub3_brb.png",
        "day_templates": {"daily": _day_template(channel)},
        "monday": "daily", "tuesday": "daily", "wednesday": "daily",
        "thursday": "daily", "friday": "daily", "saturday": "daily", "sunday": "daily",
    }
    if commercial_tag:
        conf["commercial_dir"] = commercial_tag

    # Per-daypart block length and break policy, for a channel that is not one shape all
    # day. Emitted only when something actually differs from the station setting, so a
    # normal channel's config stays as plain as it was.
    overrides: dict[str, dict] = {}
    for part in channel.dayparts:
        entry = {}
        if part.increment is not None and part.increment != (channel.increment or 30):
            entry["schedule_increment"] = part.increment
        if part.breaks is not None and part.breaks != channel.breaks:
            entry["break_strategy"] = part.breaks
        if entry:
            overrides.setdefault(part.tag, {}).update(entry)
    if overrides:
        conf["tag_overrides"] = overrides
    return {"station_conf": conf}


def apply(lineup_path: Path, media_root: Path, ads_root: Path) -> list[tuple[Channel, int]]:
    """Build every pool and write every station config. Returns (channel, episode count)."""
    from .bootstrap import build_rating_pools
    from .cards import make_station_ids

    media_root.mkdir(parents=True, exist_ok=True)
    channels = load(lineup_path)

    # Both checks before anything is written. A lineup that fails either is not partially
    # applied.
    problems = audit(channels)
    if problems:
        raise UnsafeLineup(
            "This lineup would place content not marked for children on a channel rated "
            "for children:\n  " + "\n  ".join(problems)
        )

    clashes = check_reserved(channels)
    if clashes:
        raise ReservedChannel(
            "This lineup claims a reserved channel number:\n  " + "\n  ".join(clashes)
        )

    missing = check_sources(channels)
    if missing:
        raise MissingSource(
            "This lineup names content that is not there:\n  " + "\n  ".join(missing)
        )

    pools = build_rating_pools(media_root, ads_root)
    make_station_ids(media_root / BUMP_TAG, 0, "BoobTube")

    # A tag can be shared by several channels; build each pool once. Where two channels
    # define the same tag with different exclusions, take the union — excluding more is the
    # safe direction to be wrong in, and `check_sources` has already reported any pattern
    # that matches nothing.
    built: dict[str, int] = {}
    for channel in channels:
        for tag, folders in channel.sources.items():
            if tag not in built:
                excluded: list[str] = []
                for other in channels:
                    if tag in other.sources:
                        excluded.extend(other.exclude.get(tag, []))
                built[tag] = _link_pool(media_root, tag, folders, excluded)

    out: list[tuple[Channel, int]] = []
    conf_dir = VENDOR / "confs"
    conf_dir.mkdir(parents=True, exist_ok=True)

    # Configs for channels this lineup no longer contains. Left behind, they are indis-
    # tinguishable from live ones: the supervisor keeps generating schedules for them and the
    # tuner keeps offering them, so a channel removed from the dial stays on the dial. Only
    # ever our own `tub3_chN.json` — upstream's examples live in the same directory.
    keep = {channel.station for channel in channels if channel.kind != "guide"}
    for stale in conf_dir.glob("tub3_ch*.json"):
        if stale.stem not in keep:
            stale.unlink()
    for channel in channels:
        # The guide has no pool and no station config — guidecast renders and streams it.
        # Returned anyway so it appears in the dial the CLI prints: a channel missing from
        # that list reads as an omission, which is the wrong thing to imply about the one
        # channel that is deliberately built elsewhere.
        if channel.kind == "guide":
            out.append((channel, 0))
            continue
        conf = compile_station(channel, media_root, pools=pools)
        (conf_dir / f"{channel.station}.json").write_text(json.dumps(conf, indent=4))
        episodes = sum(built.get(tag, 0) for tag in channel.sources)
        out.append((channel, episodes))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="tub3.lineup", description=__doc__)
    ap.add_argument("lineup", type=Path, help="lineup JSON")
    ap.add_argument("--media-root", type=Path, required=True)
    ap.add_argument("--ads", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    channels = load(args.lineup)
    problems = audit(channels)
    clashes = check_reserved(channels)
    missing = check_sources(channels)
    if args.dry_run:
        print()
        # Both sections, always. `apply` raises on the first of the two it meets, which is
        # right for a guard but wrong for a dry run — the point of asking first is to come
        # away with the whole list, not to fix one thing and rediscover the other.
        if problems:
            print("  REFUSED — this lineup is not safe to apply:\n")
            for problem in problems:
                print(f"    ! {problem}")
            print()
        if clashes:
            print("  REFUSED — this lineup claims a reserved channel:\n")
            for clash in clashes:
                print(f"    ! {clash}")
            print()
        if missing:
            print("  REFUSED — this lineup names content that is not there:\n")
            for gap in missing:
                print(f"    ! {gap}")
            print()
        if problems or clashes or missing:
            return 1
        print("  safety audit: passed\n")
        for channel in channels:
            if channel.kind == "guide":
                print(f"  {channel.number:>3}  {channel.name:<22} guide     "
                      f"rendered by guidecast")
                continue
            breaks = "no mid-rolls" if channel.breaks == "end" else channel.breaks
            ads = "ad-free" if channel.commercial_free else channel.rating
            print(f"  {channel.number:>3}  {channel.name:<22} {ads:<8} "
                  f"{channel.increment or 30:>3}min  {breaks}")
            for part in channel.dayparts:
                print(f"       {part.start:02d}:00-{part.end:02d}:00  {part.tag}")
        print()
        return 0

    results = apply(args.lineup, args.media_root, args.ads)
    print()
    written = 0
    for channel, episodes in results:
        if channel.kind == "guide":
            print(f"  {channel.number:>3}  {channel.name:<24}    guidecast")
            continue
        written += 1
        hours = "—"
        print(f"  {channel.number:>3}  {channel.name:<24} {episodes:>5} items  {hours}")
    print(f"\n  {written} station config(s) written to {VENDOR / 'confs'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
