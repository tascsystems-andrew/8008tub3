"""Keep the schedule ahead of the clock.

The one thing an appliance nobody administers absolutely must do. A generated schedule is
finite; when it runs out the box shows off-air cards, and the viewer's experience is that
the television broke. Nothing in FieldStation42 regenerates on its own.

Deliberately a separate short-lived process rather than a thread in the tuner:

- `StationManager` is a borg singleton that calls `exit(-1)` on a bad config, which is fatal
  in-process and merely an exit code out of it.
- `fs42/media_processor.py` calls `sys.exit(1)` at module scope when ffmpeg is missing, so
  importing FieldStation42 can end the interpreter.
- Building needs moviepy and friends; the tuner needs mpv and the standard library. Keeping
  them apart means a broken build environment cannot stop the television starting.

Two rules learned from upstream's own behaviour:

**Rebuild early, not on empty.** Upstream's panic path adds a single day and resumes from a
stale end time, so a box that has been off for a week does seven sequential day-builds with a
standby image on screen throughout. Topping up while hours remain avoids ever meeting it.

**Delete before regenerating.** Upstream writes blocks with INSERT OR REPLACE, but the only
unique constraint is an autoincrement id, so the conflict clause can never fire and a rebuild
silently doubles the schedule.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "vendor" / "FieldStation42"
DB = VENDOR / "runtime" / "fs42_fluid.db"

# Top up when fewer than this many hours remain, and build this far ahead.
LOW_WATER_HOURS = 12.0
BUILD_DAYS = 2


def _epoch(text: str) -> float:
    return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def horizons(db: Path = DB) -> dict[str, float]:
    """Hours of schedule remaining, per station."""
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT station, MAX(end_time) FROM liquid_blocks GROUP BY station"
        ).fetchall()
    finally:
        conn.close()

    now = time.time()
    out: dict[str, float] = {}
    for station, end in rows:
        if not end:
            continue
        try:
            out[station] = max(0.0, (_epoch(end) - now) / 3600.0)
        except ValueError:
            continue
    return out


def settings() -> dict:
    from .web import load_settings
    return load_settings()


def expected() -> dict[str, int]:
    """network_name -> channel, for every station that has a config.

    `horizons` reads the schedule, so a station with no blocks at all does not appear in it —
    `GROUP BY station` cannot return a row for a station that has none. Taking `min()` over
    what it returns therefore reports a healthy dial whenever the *only* station with a
    schedule is comfortably ahead, which is exactly the state a fresh lineup leaves behind:
    one channel built, nine configs with nothing behind them, and a supervisor announcing
    that all is well.

    So the set of stations that ought to exist has to come from the configs, and the schedule
    is checked against it rather than the other way round.
    """
    from .schedules import station_confs
    return {conf["network_name"]: conf["channel_number"] for conf in station_confs()}


def _build_python() -> Path:
    python = REPO / ".venv-build" / "bin" / "python"
    return python if python.exists() else Path(sys.executable)


def top_up(channels: set[int] | None = None) -> tuple[bool, str]:
    """Schedule the given stations (all of them by default) in one build process.

    Separate from `rebuild` because they answer different questions. `rebuild` builds a
    channel from a folder of media — the fresh-box path, before a lineup exists. This one
    schedules stations whose configs and pools are already on disk, which is every case after
    that, and it must stay a single process: upstream caches its directory walk per
    `content_dir`, and all these stations share one.
    """
    args = [str(_build_python()), "-m", "tub3.schedules",
            "--days", str(BUILD_DAYS),
            "--cooldown", str(settings().get("cooldown_minutes", 45))]
    for channel in sorted(channels or []):
        args += ["--channel", str(channel)]

    proc = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, "build failed: " + " / ".join(tail)
    summary = [line.strip() for line in (proc.stdout or "").splitlines()
               if "station(s) scheduled" in line]
    return True, summary[-1] if summary else "rebuilt"


def rebuild(station_channel: int = 3) -> tuple[bool, str]:
    """Build one channel from a folder of media. The fresh-box path, before any lineup."""
    conf = settings()
    programs, ads = conf.get("programs_dir"), conf.get("commercials_dir")
    if not programs or not ads:
        return False, "media folders are not set"
    if not Path(programs).exists():
        return False, f"programmes folder is unreachable: {programs}"
    if not Path(ads).exists():
        return False, f"commercials folder is unreachable: {ads}"

    python = _build_python()
    name = _station_name(station_channel)
    args = [
        str(python), "-m", "tub3.bootstrap",
        "--programs", programs,
        "--ads", ads,
        "--media-root", str(REPO / "media"),
        "--channel", str(station_channel),
        "--cooldown", str(conf.get("cooldown_minutes", 45)),
        "--days", str(BUILD_DAYS),
    ]
    if name:
        args += ["--name", name]

    proc = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, "build failed: " + " / ".join(tail)
    return True, "rebuilt"


def _station_name(channel: int) -> str | None:
    conf_path = VENDOR / "confs" / f"tub3_ch{channel}.json"
    if not conf_path.exists():
        return None
    try:
        return json.loads(conf_path.read_text())["station_conf"]["network_name"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def check_and_top_up(*, force: bool = False, quiet: bool = False) -> int:
    stations = expected()
    left = horizons()

    if not stations:
        # No configs at all: nothing has ever been built here, so there is no lineup to
        # schedule against and the single-channel path is the only one available.
        if not quiet:
            print("  no station configs — building the first channel")
        ok, message = rebuild()
        print(f"  {message}")
        return 0 if ok else 1

    # A configured station missing from the schedule has zero hours, not no opinion.
    hours = {name: left.get(name, 0.0) for name in stations}

    if not quiet:
        for name, remaining in sorted(hours.items(), key=lambda kv: stations[kv[0]]):
            mark = "LOW" if remaining < LOW_WATER_HOURS else "ok "
            note = "  (no schedule)" if name not in left else ""
            print(f"  [{mark}] {stations[name]:>3}  {name:<22} {remaining:>6.1f}h left{note}")

    stale = {stations[name] for name, remaining in hours.items()
             if remaining < LOW_WATER_HOURS}
    if not stale and not force:
        if not quiet:
            print(f"\n  every channel above the {LOW_WATER_HOURS:.0f}h mark — nothing to do\n")
        return 0

    # A forced run does the whole dial; otherwise only the channels that need it, so a
    # station with hours left is not regenerated out from under whoever is watching it.
    target = None if force else stale
    if not quiet:
        which = "every channel" if target is None else f"{len(target)} channel(s)"
        print(f"\n  topping up {which}, {BUILD_DAYS} day(s)\n")
    ok, message = top_up(target)
    print(f"  {message}\n")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.supervisor", description=__doc__)
    ap.add_argument("--once", action="store_true", help="check, top up if needed, exit")
    ap.add_argument("--force", action="store_true", help="rebuild regardless of horizon")
    ap.add_argument("--status", action="store_true", help="report and change nothing")
    args = ap.parse_args(argv)

    if args.status:
        stations = expected()
        left = horizons()
        if not stations and not left:
            print("\n  No schedule exists yet.\n")
            return 1
        print()
        for name, channel in sorted(stations.items(), key=lambda kv: kv[1]):
            hours = left.get(name)
            if hours is None:
                print(f"  {channel:>3}  {name:<22}      —  no schedule")
                continue
            until = datetime.fromtimestamp(time.time() + hours * 3600)
            print(f"  {channel:>3}  {name:<22} {hours:>6.1f}h  (until {until:%a %H:%M})")
        # A station with blocks but no config is a leftover from a previous lineup; it will
        # never be topped up again and the tuner will show it until its schedule runs out.
        for name, hours in sorted(left.items()):
            if name not in stations:
                print(f"    -  {name:<22} {hours:>6.1f}h  (orphaned — no config)")
        print()
        return 0

    return check_and_top_up(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
