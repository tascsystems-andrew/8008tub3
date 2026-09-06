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
from .lineup import AMBIANCE_CHANNEL, GUIDE_CHANNEL

HERE = Path(__file__).resolve().parent.parent
DB = HERE / "vendor" / "FieldStation42" / "runtime" / "fs42_fluid.db"


def _epoch(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def _ambiance_folder() -> Path | None:
    try:
        from .web import load_settings  # noqa: PLC0415
        folder = (load_settings() or {}).get("ambiance_dir")
        return Path(folder) if folder else None
    except Exception:  # noqa: BLE001
        return None


def channels() -> list[dict]:
    """The whole dial, not just the scheduled part of it.

    `station_confs` knows only the channels a schedule is built for. The guide and the
    ambiance loop have no config and no blocks — the tuner constructs both at boot — so a
    dial built from configs alone silently loses two channels the box plainly has.
    """
    out = [{"channel": GUIDE_CHANNEL, "station": "GUIDE", "kind": "guide"}]
    try:
        from .schedules import station_confs  # noqa: PLC0415
        out += [{"channel": c["channel_number"], "station": c["network_name"],
                 "kind": "scheduled"}
                for c in station_confs()]
    except Exception:  # noqa: BLE001
        pass
    folder = _ambiance_folder()
    if folder is not None:
        try:
            from tuner.ambiance import clips_for  # noqa: PLC0415
            if clips_for(folder):
                out.append({"channel": AMBIANCE_CHANNEL, "station": "AMBIANCE",
                            "kind": "ambiance"})
        except Exception:  # noqa: BLE001
            pass
    return sorted(out, key=lambda c: c["channel"])


# The ambiance loop is phase-locked to the clock rather than started when you tune in, for the
# same reason the rest of the dial is: a channel you join should already be running. Two people
# opening it in different rooms see the same thing, and it survives a restart.
AMBIANCE_EPOCH = datetime(2020, 1, 1).timestamp()


def _ambiance(at: float, mapping: dict | None) -> dict:
    folder = _ambiance_folder()
    from tuner.ambiance import clips_for  # noqa: PLC0415
    clips = clips_for(folder, when=at) if folder else []
    if not clips:
        return {"channel": AMBIANCE_CHANNEL, "station": "AMBIANCE", "kind": "ambiance",
                "off_air": True, "server_time": round(at, 3)}

    resolved = []
    for clip in clips:
        hit = plexmap.resolve(clip, mapping)
        resolved.append((clip, hit, float((hit or {}).get("seconds") or 0.0)))

    total = sum(seconds for _, _, seconds in resolved)
    if total <= 0:
        # No durations means no arithmetic. Play the first clip from the top rather than
        # pretending to know where in a loop we are.
        clip, hit, _ = resolved[0]
        return {"channel": AMBIANCE_CHANNEL, "station": "AMBIANCE", "kind": "ambiance",
                "server_time": round(at, 3),
                "now": {"content_type": "ambiance", "title": clip.stem, "plex": hit,
                        "offset_seconds": 0.0, "duration": 0.0, "remaining_seconds": 3600.0}}

    position = (at - AMBIANCE_EPOCH) % total
    for clip, hit, seconds in resolved:
        if position < seconds:
            return {
                "channel": AMBIANCE_CHANNEL, "station": "AMBIANCE", "kind": "ambiance",
                "server_time": round(at, 3),
                "clips": len(resolved),
                "now": {"content_type": "ambiance", "title": clip.stem, "plex": hit,
                        "offset_seconds": round(position, 2),
                        "duration": round(seconds, 2),
                        "remaining_seconds": round(seconds - position, 2)},
            }
        position -= seconds
    clip, hit, seconds = resolved[-1]     # unreachable except for float drift
    return {"channel": AMBIANCE_CHANNEL, "station": "AMBIANCE", "kind": "ambiance",
            "server_time": round(at, 3),
            "now": {"content_type": "ambiance", "title": clip.stem, "plex": hit,
                    "offset_seconds": 0.0, "duration": round(seconds, 2),
                    "remaining_seconds": round(seconds, 2)}}


def _entry(item: dict, offset: float, mapping: dict | None) -> dict:
    """One plan entry, as the app needs it."""
    path = item.get("path") or ""
    hit = plexmap.resolve(path, mapping) if path else None
    # The map says which version of a film this is, and Plex may have re-ordered them since.
    # Checked here rather than in `resolve` because this is the answer that gets *played* —
    # the same map is read by tools and tests that have no business making Plex requests.
    hit = plexmap.verify(hit, mapping) if hit else None
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
    if channel == GUIDE_CHANNEL:
        # No media and nothing to resolve: the guide *is* the listings, and the client draws
        # them from /api/guide. Saying so is the whole answer.
        return {"channel": GUIDE_CHANNEL, "station": "GUIDE", "kind": "guide",
                "server_time": round(at, 3)}
    if channel == AMBIANCE_CHANNEL:
        return _ambiance(at, plexmap.load())

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
