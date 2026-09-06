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

# **Not** the supervisor's `LOW_WATER_HOURS`, and that is the whole point.
#
# 12h is the supervisor's *control* threshold: the level at which it decides to work. A healthy
# box sawtooths 60h -> 12h -> 60h by design, and the timer only runs every six hours, so
# crossing 12h is the normal expected state of a working appliance. Alerting on it meant a
# perfectly well box showing an amber banner for hours every other day — and an alert that
# fires during normal operation is one you learn to ignore, which is worse than no alert.
#
# Four hours is a third of the way to actually running dry, well below anywhere the supervisor
# is content to leave things, and still a comfortable margin before a channel goes off air.
ALERT_HOURS = 4.0

# systemd is cheap but not free, and the box asks for this on a timer. Nothing here changes
# faster than a build, which is every six hours.
TTL = 60.0

# Two caches, not one. A single shared cache meant `health(channels)` returned whatever
# `check()` had computed from its own database read, ignoring the very list it was handed —
# so the settings page could print a banner about a starving channel directly above a grid
# showing that channel healthy, from one response. What is actually expensive is the systemd
# subprocess, so that is what gets cached; folding an already-fetched channel list into a
# verdict is arithmetic and runs fresh every time.
_build_cache: tuple[float, dict] | None = None
_check_cache: tuple[float, dict] | None = None


def configured_stations() -> set[str]:
    """The stations that ought to have a schedule, from the configs.

    Deliberately not `SELECT DISTINCT station FROM liquid_blocks`. `GROUP BY` cannot return a
    row for a station that has none, so reading the schedule to decide which stations exist
    makes a *completely* starved channel invisible — the single failure this module was
    written to catch. `supervisor.expected` already says exactly this, twenty lines from here.

    Empty means "cannot tell", and every caller treats that as unknown rather than as healthy.
    """
    try:
        from .schedules import station_confs  # noqa: PLC0415
        return {c["network_name"] for c in station_confs() if c.get("network_name")}
    except Exception:  # noqa: BLE001 - no configs is not a fault, it is no answer
        return set()


def build_state() -> dict:
    """What systemd says about the last schedule build.

    `Result` is the honest field: `ActiveState` for a `Type=oneshot` unit reads `inactive`
    whether it succeeded or failed, so asking that would call every failure healthy.
    """
    state = {"known": False, "failed": False, "missing": False, "running": False,
             "result": None, "exit_code": None, "finished": None}
    try:
        out = subprocess.run(
            ["systemctl", "show", BUILD_UNIT, "-p", "LoadState", "-p", "ActiveState",
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
    # the result would call a build unit that had been renamed or removed perfectly healthy.
    # LoadState is what knows — and this has to *raise* a fault, not merely record one. The
    # first version of this branch noted the condition and returned `failed=False`, which made
    # the guard a no-op for the exact case its own comment said it existed to catch: a removed
    # build unit read as permanently well, and nothing would ever run again.
    load = fields.get("LoadState")
    if load != "loaded":
        state["result"] = load or "not-loaded"
        state["missing"] = True
        state["failed"] = True
        return state

    # A `Type=oneshot` unit is "activating" for as long as it runs. Worth knowing, because a
    # build in flight has legitimately torn down part of the schedule it is rebuilding.
    state["running"] = fields.get("ActiveState") in ("activating", "active", "reloading")

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
           alert_hours: float | None = None,
           stations: set[str] | None = None) -> dict:
    """Fold the build result and the schedules into one verdict.

    Two rules earned the hard way.

    **The station list comes from the configs**, not from the schedule. A station with no
    blocks cannot appear in a `GROUP BY` over the schedule, so reading the schedule to decide
    which stations exist hides a completely starved channel — the one failure worth catching.

    **Starvation outranks a failed build.** A build failure is loud and recoverable: the timer
    runs again in six hours and the schedule on disk keeps playing. A channel running out is
    what actually takes the dial off air. Ranking them the other way put a red banner over a
    transient failure on a full dial and only an amber one over a dial about to go dark.
    """
    build = build_state() if build is None else build
    mark = ALERT_HOURS if alert_hours is None else alert_hours
    expected = configured_stations() if stations is None else stations

    seen = {}
    for entry in channels or []:
        name = entry.get("station")
        if name is not None:
            seen[name] = entry

    # Orphans — in the schedule but no longer configured — are deliberately not alerted on.
    # No build will ever top one up, because the supervisor only knows about configured
    # stations, so its rows sit in the past forever at 0.0h. That is a warning no action can
    # clear, and because the list sorts worst-first it would monopolise the box's single line
    # and hide every real problem behind it.
    orphans = sorted(set(seen) - expected) if expected else []
    watched = sorted(expected) if expected else sorted(seen)

    low, unknown = [], []
    for name in watched:
        entry = seen.get(name)
        if entry is None:
            low.append({"station": name, "hours": 0.0, "reason": "no schedule at all"})
            continue
        hours = entry.get("schedule_hours_left")
        if hours is None:
            # A row exists but its end time could not be read. Say so rather than guessing
            # either way — silently skipping it reports a starving channel as healthy.
            unknown.append(name)
        elif hours < mark:
            low.append({"station": name, "hours": hours, "reason": "low"})
    low.sort(key=lambda item: (item["hours"], item["station"]))

    messages = []
    if build.get("missing"):
        messages.append(
            f"The schedule build unit is {build.get('result')} — no build will ever run."
        )
    elif build.get("failed"):
        code = build.get("exit_code")
        detail = str(build.get("result")) + (f", exit {code}" if code else "")
        messages.append(f"The last schedule build failed ({detail}).")

    # A build in flight has legitimately cleared part of what it is rebuilding, so the horizon
    # dips below the mark every time one runs. Warning then is warning about the fix.
    building = bool(build.get("running"))
    if low and not building:
        for item in low:
            if item["reason"] == "no schedule at all":
                messages.append(f"{item['station']} has no schedule at all.")
            else:
                messages.append(
                    f"{item['station']} has {item['hours']:.1f}h of schedule left, "
                    f"under the {mark:.0f}h mark."
                )
    for name in unknown:
        messages.append(f"{name} has a schedule that cannot be read.")

    starving = bool(low) and not building
    if starving or build.get("missing"):
        level = "fault"
    elif build.get("failed") or unknown:
        level = "warning"
    else:
        level = "ok"

    return {
        "level": level,
        "messages": messages,
        "build": build,
        "low_channels": [] if building else low,
        "orphans": orphans,
        "unknown": unknown,
        "building": building,
        "alert_hours": mark,
        "low_water_hours": mark,      # kept: the settings page reads this name
        "checked_at": time.time(),
    }


def cached_build_state(*, force: bool = False) -> dict:
    """`build_state`, cached — the one genuinely expensive call in this module."""
    global _build_cache
    now = time.time()
    if not force and _build_cache is not None and now - _build_cache[0] < TTL:
        return _build_cache[1]
    state = build_state()
    _build_cache = (now, state)
    return state


def health(channels: list[dict], *, force: bool = False) -> dict:
    """A verdict about *these* channels. Never served from a cache of other channels."""
    return assess(channels, build=cached_build_state(force=force))


def check(*, force: bool = False) -> dict:
    """Health without being handed anything — reads the schedule database itself.

    Consults the cache before touching the database, not after. Written the other way round
    it opened and queried sqlite on every call and then threw the answer away, which on the
    tuner meant a database read every time the menu redrew.
    """
    global _check_cache
    now = time.time()
    if not force and _check_cache is not None and now - _check_cache[0] < TTL:
        return _check_cache[1]
    verdict = assess(schedule_hours(), build=cached_build_state(force=force))
    _check_cache = (now, verdict)
    return verdict
