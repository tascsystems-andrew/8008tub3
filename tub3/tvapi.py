"""What is on a channel right now, in terms an app can act on.

The box answers this for itself by opening a file and seeking. A phone or an Apple TV cannot
do that — it asks Plex for an item and a start offset. Same question, different vocabulary,
so this translates: block -> plan entry -> file -> Plex `ratingKey` -> seconds in.

Three things are deliberately not here. It serves no video: Plex already decides far better
than a Pi 5 can whether to send bytes untouched, remux, or transcode, and the Pi 5 has no
hardware encoder to argue with. It holds no Plex token: the box does not keep one, and an app
should be a Plex client in its own right so it works away from the house. And it does no
scheduling: the schedule already exists in `liquid_blocks`, built by the supervisor, and a
second opinion about what is on would be a second opinion too many.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import plexmap

HERE = Path(__file__).resolve().parent.parent
DB = HERE / "vendor" / "FieldStation42" / "runtime" / "fs42_fluid.db"


def _epoch(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def channels() -> list[dict]:
    """The dial, from the station configs — the same source the tuner numbers it from."""
    try:
        from .schedules import station_confs  # noqa: PLC0415
        return [{"channel": c["channel_number"], "station": c["network_name"]}
                for c in station_confs()]
    except Exception:  # noqa: BLE001
        return []


def _entry(item: dict, offset: float, mapping: dict | None) -> dict:
    """One plan entry, as the app needs it."""
    path = item.get("path") or ""
    hit = plexmap.resolve(path, mapping) if path else None
    duration = float(item.get("duration") or 0)
    # `skip` is how far into the file this entry starts — a programme split around an ad break
    # resumes partway in, and an app that ignored it would replay the first half.
    skip = float(item.get("skip") or 0)
    return {
        "content_type": item.get("content_type"),
        "duration": round(duration, 2),
        "offset_seconds": round(skip + offset, 2),
        "remaining_seconds": round(max(0.0, duration - offset), 2),
        "plex": hit,                       # None when Plex cannot identify the file
        "title": Path(path).stem or None,
    }


def now(channel: int, at: float | None = None) -> dict:
    """What channel `channel` is playing, and how far into it."""
    at = time.time() if at is None else at
    dial = {c["channel"]: c["station"] for c in channels()}
    station = dial.get(channel)
    if station is None:
        return {"error": f"channel {channel} is not on the dial", "channel": channel}

    if not DB.exists():
        return {"error": "no schedule database", "channel": channel, "station": station}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"error": str(exc), "channel": channel, "station": station}
    try:
        row = conn.execute(
            "SELECT start_time, end_time, title, plan_json FROM liquid_blocks "
            "WHERE station = ? AND start_time <= ? AND end_time > ? LIMIT 1",
            (station, datetime.fromtimestamp(at), datetime.fromtimestamp(at)),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        return {"error": str(exc), "channel": channel, "station": station}
    finally:
        conn.close()

    if not row:
        return {"channel": channel, "station": station, "off_air": True,
                "server_time": round(at, 3)}

    start, end, title, plan = row
    try:
        entries = json.loads(plan)
    except (TypeError, json.JSONDecodeError):
        entries = []

    mapping = plexmap.load()
    elapsed = at - _epoch(start)
    running = 0.0
    current = nxt = None
    for i, item in enumerate(entries):
        span = float(item.get("duration") or 0)
        if running + span > elapsed:
            current = _entry(item, elapsed - running, mapping)
            if i + 1 < len(entries):
                nxt = _entry(entries[i + 1], 0.0, mapping)
            break
        running += span

    return {
        "channel": channel,
        "station": station,
        "block_title": title,
        "block_ends_at": round(_epoch(end), 3),
        "server_time": round(at, 3),
        "now": current,
        "next": nxt,
        "map": None if not mapping else {
            "built_at": mapping.get("built_at"),
            "keys": (mapping.get("counts") or {}).get("keys"),
            "stale": bool(mapping.get("stale")),
        },
    }
