"""Generate schedules for every station on the dial, in one process.

`bootstrap` builds *a* channel: it arranges media, writes one config, and schedules it. That
conflates two jobs, and only the second one generalises. Once `lineup.apply` has written nine
station configs and their symlink pools, the media layout is already done — what is missing is
a schedule per station, and asking `bootstrap` for it would rebuild the layout nine times.

**One process, deliberately.** Upstream caches its directory walk on a class attribute keyed
by `(realpath(content_dir), media_filter)` (`fs42/catalog.py:128`), and every station this
project writes shares one `content_dir` — the tag pools live side by side under it. So the
expensive scan happens once for the first station and every station after it skips straight
to indexing. Ten `subprocess` calls would pay for that walk ten times over; on a NAS that is
the difference between minutes and most of an hour.

The same property is why `clear_fluid_cache()` is called exactly once, at the start, rather
than before each station: clearing it between stations would defeat the sharing it exists to
provide, and *not* clearing it at all would let a rebuild miss files added since the process
started — which is only a problem in a long-running process, but this one is long enough.

**Only what needs it.** Regenerating a station that still has hours left would change what is
on air mid-programme for no reason, so the caller passes the channels to touch and the rest
are left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .bootstrap import VENDOR, _placeholder_image


def station_confs() -> list[dict]:
    """Every station config on disk, lowest channel first.

    The guide has no config by design — `lineup.compile_station` refuses to write one, since
    it is rendered by guidecast rather than scheduled — so it simply never appears here.
    """
    out = []
    for path in sorted((VENDOR / "confs").glob("tub3_ch*.json")):
        try:
            conf = json.loads(path.read_text()).get("station_conf", {})
        except (json.JSONDecodeError, OSError):
            continue
        if conf.get("channel_number") is not None and conf.get("network_name"):
            out.append(conf)
    return sorted(out, key=lambda c: c["channel_number"])


def _clear_blocks(name: str) -> int:
    """Drop a station's existing blocks. Returns how many went.

    Upstream writes with INSERT OR REPLACE, but the only unique constraint is an
    autoincrement id and every index is non-UNIQUE, so the conflict clause can never fire and
    a rebuild silently doubles the schedule.

    The guard matters: `liquid_blocks` does not exist until upstream writes its first
    schedule, well after the catalog is built. On a fresh box an unguarded DELETE turns the
    very first run into a traceback after forty minutes of cataloguing — and only the first
    run, which is how it survives development.
    """
    with sqlite3.connect("runtime/fs42_fluid.db") as conn:
        try:
            return conn.execute(
                "DELETE FROM liquid_blocks WHERE station = ?", (name,)
            ).rowcount
        except sqlite3.OperationalError:
            return 0


def _say(*parts) -> None:
    """Progress, flushed.

    This runs for tens of minutes behind a redirect, where Python block-buffers stdout and
    nothing appears until the buffer fills. A long build with no output is indistinguishable
    from a hung one — and the person waiting is looking at a standby card with no way to tell
    which they have.
    """
    print(*parts, flush=True)


def build_titles(media_root: Path) -> tuple[int, str]:
    """Write the programme-name map the tuner reads at tune time.

    `tuner.titles.build` has existed since the bug was written and nothing ever called it, so
    `titles.json` on the box was two bytes — `{}` — and every name on screen came from the
    filename parser that the map exists to make unnecessary. It was not returning nothing; it
    was never asked.

    Belongs here because it is a build-side job with the same shape as everything else in this
    module: it needs Plex and a directory walk, both of which the tuner deliberately refuses,
    and the answer must be sitting on local disk before a channel change asks for it. Failures
    are reported and never raised — a missing episode title costs one line on the bug, and
    must not be able to fail a schedule.
    """
    from tuner.titles import TITLES, build
    from .plex import from_config

    try:
        client = from_config()
    except Exception as exc:  # noqa: BLE001
        return 0, f"Plex config unreadable: {exc}"
    if client is None:
        return 0, "Plex is not configured — names will come from filenames"

    try:
        mapping = build(media_root, client)
    except Exception as exc:  # noqa: BLE001
        return 0, f"title map failed: {exc}"

    try:
        TITLES.write_text(json.dumps(mapping, indent=1))
    except OSError as exc:
        return 0, f"could not write {TITLES}: {exc}"

    episodes = sum(1 for entry in mapping.values() if entry.get("episode"))
    return len(mapping), f"{len(mapping)} titles ({episodes} with episode names)"


def schedule_all(
    days: int = 2,
    *,
    cooldown: int = 45,
    only: set[int] | None = None,
    smallest_first: bool = True,
    quiet: bool = False,
) -> tuple[int, list[str]]:
    """Catalog and schedule each station. Returns (stations done, problems)."""
    confs = station_confs()
    if not confs:
        return 0, ["no station configs — run tub3.lineup first"]

    wanted = [c for c in confs if only is None or c["channel_number"] in only]
    if not wanted:
        return 0, []

    if smallest_first:
        # Cheapest station first, so channels appear steadily rather than all at the end.
        # Counting links is a local directory walk over symlinks — no NAS traffic — and it is
        # a good enough proxy for how long the scan behind it will take.
        def weight(conf: dict) -> int:
            """How many spots and episodes this station draws on. Names only, never stats.

            Two wrong versions preceded this one, and both were the same misunderstanding of
            what a pool is — a directory of symlinks to files on the NAS.

            `Path.rglob` does not descend through a symlinked directory, so it counted each
            station's *tags*: a two-tag channel with a thousand episodes sorted ahead of a
            four-tag channel with two hundred, and the ordering did the exact opposite of its
            job. `os.walk(followlinks=True)` fixed the counting and broke something worse — it
            calls `is_dir()` on every entry, which follows each link through to the NAS. That
            is eleven thousand stat calls across a saturated mount to sort a list of ten, and
            it wedged the build in disk-sleep before it printed a single line.

            `os.listdir` returns names without stat'ing any of them, so this stays entirely
            local. Nested bump directories count as two entries rather than their contents,
            which is a rounding error against pools of hundreds.
            """
            root = conf["content_dir"]
            total = 0
            try:
                for tag in os.listdir(root):
                    try:
                        total += len(os.listdir(os.path.join(root, tag)))
                    except OSError:
                        continue
            except OSError:
                return 1 << 30
            return total
        wanted.sort(key=weight)
        _say("  order: " + " → ".join(f"{c['channel_number']}" for c in wanted))

    # Upstream resolves the schedule DB, confs/ and its config schema against the process
    # working directory, and importing the package is separate from being able to find them.
    # Both are needed and they fail at different points if you only do one.
    os.chdir(VENDOR)
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))

    _placeholder_image(Path("runtime/tub3_standby.png"), "PLEASE", "STAND BY")
    _placeholder_image(Path("runtime/tub3_brb.png"), "WE WILL", "RETURN")

    from fs42.catalog import ShowCatalog              # noqa: PLC0415 - after chdir
    from fs42.liquid_schedule import LiquidSchedule   # noqa: PLC0415
    from fs42.station_manager import StationManager   # noqa: PLC0415

    from .adcatalog import install                    # noqa: PLC0415

    # A borg that loads every config in confs/ during __init__ and calls exit(-1) on a bad
    # one, so it must only ever run in a short-lived, supervised process — which this is.
    manager = StationManager()

    # Once, not per station: see the module docstring.
    ShowCatalog.clear_fluid_cache()

    problems: list[str] = []
    done = 0
    for conf in wanted:
        channel = conf["channel_number"]
        name = conf["network_name"]
        station_conf = manager.station_by_channel(channel)
        if station_conf is None:
            problems.append(f"channel {channel} ({name}) was rejected upstream")
            continue

        if not quiet:
            _say(f"\n  ch {channel:<3} {name}")

        # Ad selection is driven by class attributes on our catalog subclass, so the pool has
        # to be re-bound for each station — they do not all draw from the same rating. The
        # cluster computation is cached on disk, so revisiting a pool a second time is cheap.
        commercial_tag = conf.get("commercial_dir")
        pool = Path(conf["content_dir"]) / commercial_tag if commercial_tag else None
        install(pool, cooldown_minutes=cooldown)

        try:
            catalog = ShowCatalog(station_conf, rebuild_catalog=True)
        except Exception as exc:  # noqa: BLE001 - one bad station must not take the dial down
            problems.append(f"channel {channel} ({name}) catalog failed: {exc}")
            continue

        items = sum(
            len(entries) if isinstance(entries, (list, tuple)) else 1
            for entries in (catalog.clip_index or {}).values()
        )
        removed = _clear_blocks(name)
        if not quiet:
            _say(f"         catalog {items} item(s)"
                 + (f", cleared {removed} old block(s)" if removed > 0 else ""))

        try:
            schedule = LiquidSchedule(station_conf)
            schedule.add_days(days)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"channel {channel} ({name}) schedule failed: {exc}")
            continue

        used = getattr(schedule, "catalog", None)
        if not quiet and used is not None and hasattr(used, "repeats_prevented"):
            relaxed = sum(used.relaxations.values())
            _say(f"         ads     {used.repeats_prevented} at full cooldown, "
                 f"{relaxed} relaxed, {used.starved} unconstrained")
        done += 1

    # After the stations, because it walks the same pools they were just built from and a
    # name on screen matters less than a channel existing to put it on.
    if done:
        # `content_dir` is now a per-station subtree (`.../media/st12`), so the pools all
        # channels share live one level up. Titles are mapped for the whole dial at once —
        # the map is keyed by resolved file path and the tuner reads one file, so building it
        # per station would rewrite the same JSON ten times with progressively more in it.
        count, note = build_titles(Path(wanted[0]["content_dir"]).parent)
        if not quiet:
            _say(f"\n  titles  {note}")
        if not count:
            problems.append(f"title map not built — {note}")

    return done, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.schedules", description=__doc__)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--cooldown", type=int, default=45)
    ap.add_argument("--channel", type=int, action="append",
                    help="only this channel; repeatable. Default is every station.")
    ap.add_argument("--list", action="store_true", help="show the stations and change nothing")
    ap.add_argument("--titles-only", action="store_true",
                    help="rebuild the programme-name map without touching any schedule")
    args = ap.parse_args(argv)

    if args.titles_only:
        confs = station_confs()
        if not confs:
            print("\n  no station configs — run tub3.lineup first\n", file=sys.stderr)
            return 1
        count, note = build_titles(Path(confs[0]["content_dir"]).parent)
        print(f"\n  {note}\n")
        return 0 if count else 1

    if args.list:
        print()
        for conf in station_confs():
            print(f"  {conf['channel_number']:>3}  {conf['network_name']:<22} "
                  f"{conf.get('schedule_increment', 30):>3}min  "
                  f"{'ad-free' if conf.get('commercial_free') else conf.get('commercial_dir', '')}")
        print()
        return 0

    done, problems = schedule_all(
        args.days, cooldown=args.cooldown,
        only=set(args.channel) if args.channel else None,
    )
    print(f"\n  {done} station(s) scheduled")
    for problem in problems:
        print(f"  ! {problem}", file=sys.stderr)
    print()
    return 1 if problems and done == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
