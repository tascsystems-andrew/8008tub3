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

from tuner.titles import describe

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
    from tuner.ambiance import clips_for, seconds_to_next_daypart  # noqa: PLC0415
    clips = clips_for(folder, when=at) if folder else []
    # The slot never outlives the hour that chose it. Without this a client is handed a
    # three-hour rain loop at half past three and is still raining at bedtime, while the
    # television beside it changed at four: the box re-tunes on its own run loop, and an
    # HTTP client has nothing to notice a boundary with.
    left = seconds_to_next_daypart(at)
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
                        "offset_seconds": 0.0, "duration": 0.0,
                        "remaining_seconds": round(min(3600.0, left), 2)}}

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
                        "remaining_seconds": round(min(seconds - position, left), 2)},
            }
        position -= seconds
    clip, hit, seconds = resolved[-1]     # unreachable except for float drift
    return {"channel": AMBIANCE_CHANNEL, "station": "AMBIANCE", "kind": "ambiance",
            "server_time": round(at, 3),
            "now": {"content_type": "ambiance", "title": clip.stem, "plex": hit,
                    "offset_seconds": 0.0, "duration": round(seconds, 2),
                    "remaining_seconds": round(min(seconds, left), 2)}}


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
    # The real programme name, the same way the television gets it.
    #
    # `tuner.box` has used `describe` for its own channel bug since that bug existed, and
    # `tuner.guide` for the listings — so the Pi showed "This Old House / The Reading House"
    # while this endpoint handed the app
    # "thisoldhouse__This Old House - S08E08 - The Reading House - 8 WEBDL-1080p", a pool
    # prefix and a scene suffix wrapped around the answer. The app had built a regex to strip
    # the prefix and swap dots for spaces, which is the client guessing at something the box
    # already knew.
    #
    # Free at request time: `describe` reads a flat JSON map built at schedule time, so this
    # costs a dict lookup and no Plex round trip. That is the whole point of titles.json.
    show, episode = describe(path) if path else ("", "")
    return {
        "content_type": item.get("content_type"),
        "duration": round(duration, 2),
        "offset_seconds": round(skip + offset, 2),
        "remaining_seconds": round(max(0.0, duration - offset), 2),
        "plex": hit,                       # None when Plex cannot identify the file
        # Kept, and still the pool stem: an older build of the app reads it and a field that
        # changes meaning under a client is worse than one that is merely redundant.
        "title": Path(path).stem or None,
        "show": show or None,
        "episode": episode or None,
    }


# What counts as an interruption rather than the programme. Imported from the tuner rather
# than restated, because two lists of break types is how the television and the app end up
# disagreeing about whether an ident is a programme.
from tuner.schedule import BREAK_TYPES  # noqa: E402


def _feature_index(entries: list, index: int) -> int:
    """Which entry is the *programme*, seen from `index`. `tuner.schedule._feature_slot`.

    Backwards first: during a mid-roll the programme is the thing that was already playing,
    and the viewer's question is "what am I watching", not "what is this advert". Forwards is
    the fallback for a block that opens with an ident, where there is nothing behind us yet.
    """
    if not entries or not (0 <= index < len(entries)):
        return index
    if (entries[index].get("content_type") or "") not in BREAK_TYPES:
        return index
    for position in range(index - 1, -1, -1):
        if (entries[position].get("content_type") or "") not in BREAK_TYPES:
            return position
    for position in range(index + 1, len(entries)):
        if (entries[position].get("content_type") or "") not in BREAK_TYPES:
            return position
    return index


def _feature(entries: list, index: int, offset: float, mapping: dict | None) -> dict | None:
    """The programme, and how long until it actually finishes — ads included.

    The television has drawn its bug from this since somebody noticed it was captioning the
    picture with the name of a commercial. `/api/tv/N/now` never got the same treatment, so
    the app kept doing exactly that: `now` is the entry on screen, which during a break is the
    advert. Its own comment in tuner/schedule.py says it plainly — "correct for playback and
    wrong for the bug".

    `remaining_seconds` walks forward to the last entry that is the same file and counts the
    breaks in between, because a 23-minute episode cut into four pieces would otherwise say
    "3 min left" eighteen minutes before it ends. It is counting to the next commercial, and
    reads as counting to the end of the show.
    """
    if not entries:
        return None
    slot = _feature_index(entries, index)
    item = entries[slot]
    path = item.get("path") or ""
    show, episode = describe(path) if path else ("", "")

    last = slot
    for position in range(slot + 1, len(entries)):
        if (entries[position].get("path") or "") == path:
            last = position
    if last == slot:
        remaining = max(0.0, float(item.get("duration") or 0) - (offset if slot == index else 0.0))
    else:
        total = sum(float(entries[p].get("duration") or 0) for p in range(slot, last + 1))
        # How far into the programme's own span we are, counting the breaks already shown.
        gone = sum(float(entries[p].get("duration") or 0) for p in range(slot, index)) + offset
        remaining = max(0.0, total - gone)

    return {
        "show": show or None,
        "episode": episode or None,
        "content_type": item.get("content_type"),
        "remaining_seconds": round(remaining, 2),
        "in_break": slot != index,
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
    current = nxt = feature = None
    for i, item in enumerate(entries):
        span = float(item.get("duration") or 0)
        if running + span > elapsed:
            current = _entry(item, elapsed - running, mapping)
            feature = _feature(entries, i, elapsed - running, mapping)
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
        # What is actually on, as opposed to what is on screen this second. During a break
        # `now` is the advert; this is the programme it is interrupting.
        "feature": feature,
        "next": nxt,
        "map": None if not mapping else {
            "built_at": mapping.get("built_at"),
            "keys": (mapping.get("counts") or {}).get("keys"),
            "stale": bool(mapping.get("stale")),
        },
    }
