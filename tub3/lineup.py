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


@dataclass
class Daypart:
    """Which tag airs during which hours. `hours` is inclusive-start, exclusive-end."""

    start: int
    end: int
    tag: str

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
    # tag -> the folders whose episodes belong to it
    sources: dict[str, list[str]] = field(default_factory=dict)
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
            dayparts.append(Daypart(int(start), int(end), part["tag"]))
        channels.append(Channel(
            number=int(raw["number"]),
            name=raw["name"],
            rating=raw.get("rating", "late"),
            sources={k: list(v) for k, v in raw.get("sources", {}).items()},
            dayparts=dayparts,
            increment=raw.get("increment"),
            break_duration=int(raw.get("break_duration", 120)),
            breaks=raw.get("breaks", "standard"),
            commercial_free=bool(raw.get("commercial_free", False)),
        ))
    return sorted(channels, key=lambda c: c.number)


class UnsafeLineup(ValueError):
    """A proposed lineup would put adult content where children can reach it."""


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

    problems: list[str] = []
    for channel in channels:
        if channel.rating not in ("kids", "family"):
            continue
        for tag, folders in channel.sources.items():
            for folder in folders:
                path = Path(folder)
                rating, why = classify(path.parent.name, path.name, path)
                if rating != "kids":
                    problems.append(
                        f"channel {channel.number} ({channel.name!r}) is rated "
                        f"{channel.rating!r} but tag {tag!r} draws from {folder!r}, "
                        f"which is rated {rating} — {why}"
                    )
    return problems


def _link_pool(media_root: Path, tag: str, folders: list[str]) -> int:
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

    linked = 0
    for folder in folders:
        source = Path(folder)
        if not source.exists():
            continue
        prefix = _normalise(source.name)[:28]
        for item in sorted(source.rglob("*")):
            if not item.is_file() or item.name.startswith("."):
                continue
            if item.suffix.lower() not in VIDEO_SUFFIXES:
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
    return {"station_conf": conf}


def apply(lineup_path: Path, media_root: Path, ads_root: Path) -> list[tuple[Channel, int]]:
    """Build every pool and write every station config. Returns (channel, episode count)."""
    from .bootstrap import build_rating_pools
    from .cards import make_station_ids

    media_root.mkdir(parents=True, exist_ok=True)
    channels = load(lineup_path)

    # Before anything is written. A lineup that fails this is not partially applied.
    problems = audit(channels)
    if problems:
        raise UnsafeLineup(
            "This lineup would place content not marked for children on a channel rated "
            "for children:\n  " + "\n  ".join(problems)
        )

    pools = build_rating_pools(media_root, ads_root)
    make_station_ids(media_root / BUMP_TAG, 0, "BoobTube")

    # A tag can be shared by several channels; build each pool once.
    built: dict[str, int] = {}
    for channel in channels:
        for tag, folders in channel.sources.items():
            if tag not in built:
                built[tag] = _link_pool(media_root, tag, folders)

    out: list[tuple[Channel, int]] = []
    conf_dir = VENDOR / "confs"
    conf_dir.mkdir(parents=True, exist_ok=True)
    for channel in channels:
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
    if args.dry_run:
        print()
        if problems:
            print("  REFUSED — this lineup is not safe to apply:\n")
            for problem in problems:
                print(f"    ! {problem}")
            print()
            return 1
        print("  safety audit: passed\n")
        for channel in channels:
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
    for channel, episodes in results:
        hours = "—"
        print(f"  {channel.number:>3}  {channel.name:<24} {episodes:>5} items  {hours}")
    print(f"\n  {len(results)} station config(s) written to {VENDOR / 'confs'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
