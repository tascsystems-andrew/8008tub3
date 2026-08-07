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


def rebuild(station_channel: int = 3) -> tuple[bool, str]:
    """Run bootstrap in its own process, with the build interpreter."""
    conf = settings()
    programs, ads = conf.get("programs_dir"), conf.get("commercials_dir")
    if not programs or not ads:
        return False, "media folders are not set"
    if not Path(programs).exists():
        return False, f"programmes folder is unreachable: {programs}"
    if not Path(ads).exists():
        return False, f"commercials folder is unreachable: {ads}"

    python = REPO / ".venv-build" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)

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
    left = horizons()
    if not left:
        if not quiet:
            print("  no schedule at all — building")
        ok, message = rebuild()
        print(f"  {message}")
        return 0 if ok else 1

    lowest = min(left.values())
    if not quiet:
        for station, hours in sorted(left.items()):
            mark = "LOW" if hours < LOW_WATER_HOURS else "ok "
            print(f"  [{mark}] {station:<22} {hours:>6.1f}h left")

    if lowest >= LOW_WATER_HOURS and not force:
        if not quiet:
            print(f"\n  {lowest:.1f}h remaining, above the {LOW_WATER_HOURS:.0f}h mark — "
                  f"nothing to do\n")
        return 0

    if not quiet:
        print(f"\n  {lowest:.1f}h left — topping up {BUILD_DAYS} day(s)\n")
    ok, message = rebuild()
    print(f"  {message}\n")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tub3.supervisor", description=__doc__)
    ap.add_argument("--once", action="store_true", help="check, top up if needed, exit")
    ap.add_argument("--force", action="store_true", help="rebuild regardless of horizon")
    ap.add_argument("--status", action="store_true", help="report and change nothing")
    args = ap.parse_args(argv)

    if args.status:
        left = horizons()
        if not left:
            print("\n  No schedule exists yet.\n")
            return 1
        print()
        for station, hours in sorted(left.items()):
            until = datetime.fromtimestamp(time.time() + hours * 3600)
            print(f"  {station:<22} {hours:>6.1f}h  (until {until:%a %H:%M})")
        print()
        return 0

    return check_and_top_up(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
