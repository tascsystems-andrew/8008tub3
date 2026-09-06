"""Whether the box is quietly broken, and how to say so.

The dial degrades gracefully almost everywhere — a missing month borrows a neighbour, a
missing clip falls back, a dead channel is left off rather than shown black. That is the right
behaviour and it has one cost: **nothing looks wrong until it is very wrong.** A schedule build
can fail every six hours for two days and the only symptom is channels going off air one by
one, long after the cause.

Two things are worth saying out loud, and they are not the same thing:

* **The build failed.** Loud, easy, and on its own not urgent — the timer runs again in six
  hours and the schedule on disk keeps playing until then.
* **A channel is running out of schedule.** Quiet, and the one that actually takes the dial off
  air. It does not require a build to have failed: a build that exits 0 having topped up
  nothing looks identical to a healthy one from the outside. That is not hypothetical — a
  Sonarr rename left 57 dead symlinks and channels starved while every build "succeeded".

Deliberately no push service, no mail, no credentials: this is read by the settings page and
by the box's own menu. Andrew's call, and it keeps the appliance self-contained.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

BUILD_UNIT = "tub3-build.service"

# The same database the build writes and the settings page reads. Resolved here rather than
# imported from `web` so that the tuner can ask about health without pulling in an HTTP
# server, and so a box with the settings page stopped still knows whether it is starving.
VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42"
DB = VENDOR / "runtime" / "fs42_fluid.db"

# Matches `tub3.supervisor.LOW_WATER_HOURS`. Imported lazily rather than referenced directly so
# that this module stays importable on a desktop, where the supervisor's dependencies are not
# installed and health is still worth reporting.
DEFAULT_LOW_WATER_HOURS = 12.0

# systemd is cheap but not free, and the box asks for this on a timer. Nothing here changes
# faster than a build, which is every six hours.
TTL = 60.0

_cache: tuple[float, dict] | None = None


def _low_water() -> float:
    try:
        from .supervisor import LOW_WATER_HOURS  # noqa: PLC0415
        return float(LOW_WATER_HOURS)
    except Exception:  # noqa: BLE001 - a desktop without the build venv still reports health
        return DEFAULT_LOW_WATER_HOURS


def build_state() -> dict:
    """What systemd says about the last schedule build.

    `Result` is the honest field: `ActiveState` for a `Type=oneshot` unit reads `inactive`
    whether it succeeded or failed, so asking that would call every failure healthy.
    """
    state = {"known": False, "failed": False, "result": None,
             "exit_code": None, "finished": None}
    try:
        out = subprocess.run(
            ["systemctl", "show", BUILD_UNIT, "-p", "LoadState",
             "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"],
            capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return state
    if out.returncode != 0:
        return state

    fields = {}
    for line in out.stdout.splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()

    # A unit that does not exist answers `Result=success` with exit 0, so asking only about
    # the result would call a build unit that had been renamed or removed perfectly healthy —
    # silence for the one reason nobody would ever think to check. LoadState is what knows.
    if fields.get("LoadState") != "loaded":
        state["result"] = fields.get("LoadState") or "not-loaded"
        return state

    result = fields.get("Result") or None
    if result is None:
        return state
    state["known"] = True
    state["result"] = result
    try:
        state["exit_code"] = int(fields.get("ExecMainStatus") or 0)
    except ValueError:
        state["exit_code"] = None
    stamp = fields.get("ExecMainExitTimestamp") or ""
    if stamp:
        for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
            try:
                state["finished"] = time.mktime(time.strptime(stamp, fmt))
                break
            except ValueError:
                continue
    # "success" is the only healthy value; everything else — exit-code, signal, timeout,
    # oom-kill, core-dump — is a failure and should be named rather than lumped together.
    state["failed"] = result != "success"
    return state


def schedule_hours(db: Path | None = None) -> list[dict]:
    """Hours of schedule left per station, straight from the build's own database.

    Shaped like `web.channel_status()` so `assess` can take either. Read-only and read-only
    by URI, so a half-written database during a build is a missing answer rather than a
    corrupt one — and a missing answer is reported as unknown, never as starving.
    """
    path = DB if db is None else db
    if not Path(path).exists():
        return []
    now = time.time()
    out: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT station, MAX(end_time), COUNT(*) FROM liquid_blocks GROUP BY station"
        ).fetchall()
    except sqlite3.DatabaseError:
        # Normal for several minutes during a build: the catalog tables are written well
        # before `liquid_blocks` exists.
        return []
    finally:
        conn.close()

    for station, end, blocks in rows:
        entry = {"station": station, "blocks": blocks}
        if end:
            try:
                stamp = end if isinstance(end, (int, float)) else datetime.fromisoformat(
                    str(end)).timestamp()
            except ValueError:
                continue
            entry["schedule_hours_left"] = round(max(0.0, (stamp - now) / 3600.0), 1)
        out.append(entry)
    return out


def assess(channels: list[dict], build: dict | None = None,
           low_water: float | None = None) -> dict:
    """Fold the build result and the channel schedules into one verdict."""
    build = build_state() if build is None else build
    mark = _low_water() if low_water is None else low_water

    low = []
    for entry in channels or []:
        hours = entry.get("schedule_hours_left")
        if hours is None:                     # a channel with no schedule at all
            if entry.get("blocks"):
                continue
            low.append({"station": entry.get("station", "?"), "hours": 0.0})
        elif hours < mark:
            low.append({"station": entry.get("station", "?"), "hours": hours})
    low.sort(key=lambda item: item["hours"])

    messages = []
    if build.get("failed"):
        code = build.get("exit_code")
        detail = f"{build.get('result')}" + (f", exit {code}" if code else "")
        messages.append(f"The last schedule build failed ({detail}).")
    for item in low:
        messages.append(
            f"{item['station']} has {item['hours']:.1f}h of schedule left, "
            f"under the {mark:.0f}h mark."
        )

    if build.get("failed"):
        level = "fault"
    elif low:
        level = "warning"
    else:
        level = "ok"

    return {
        "level": level,
        "messages": messages,
        "build": build,
        "low_channels": low,
        "low_water_hours": mark,
        "checked_at": time.time(),
    }


def check(*, force: bool = False) -> dict:
    """Health without being handed anything — reads the schedule database itself.

    Consults the cache *before* touching the database, not after. Written the other way round
    this opened and queried sqlite on every call and then threw the answer away, which on the
    tuner would mean a database read every time the menu redrew.
    """
    global _cache
    now = time.time()
    if not force and _cache is not None and now - _cache[0] < TTL:
        return _cache[1]
    return health(schedule_hours(), force=True)


def health(channels: list[dict], *, force: bool = False) -> dict:
    """`assess`, cached, because the box asks on a timer and systemd is a subprocess."""
    global _cache
    now = time.time()
    if not force and _cache is not None and now - _cache[0] < TTL:
        return _cache[1]
    verdict = assess(channels)
    _cache = (now, verdict)
    return verdict
