"""What a file on the dial is called in Plex.

The box plays files. An app on an iPad or an Apple TV cannot open a file on the NAS — it asks
Plex for an item by `ratingKey` and Plex decides whether to send the bytes untouched, remux
them, or transcode. So the bridge between the two worlds is one question: *given the path this
block is about to play, which Plex item is that?*

`plex.py` already answers it — it reads `Part file=` out of Plex and matches case-folded
against the tail of a path. What it cannot do is answer it quickly. Building the indexes means
one request per show, and this library has 127 of them holding 43,170 episodes; a minute is
fine once and hopeless per request.

So the answer is computed once and written down. The map is a plain dict of path-key to rating
key — derived data, thrown away and rebuilt whenever it looks stale, and never the source of
truth for anything.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import plex as plexmod

HERE = Path(__file__).resolve().parent.parent
MAP_FILE = HERE / "runtime" / "plexmap.json"

# A day. The library changes when Sonarr lands an episode, which is often, but a miss costs
# one unresolved item and the box keeps playing regardless — so this trades freshness for not
# hammering Plex with a full crawl.
MAX_AGE = 86400.0

# Bumped whenever the *meaning* of an entry changes, not its shape. Version 2 attaches the
# real `media_index` to a film rather than always 0, so a map built before it is not merely
# old, it is wrong — and nothing here rebuilds an old map, it only marks it stale and hands
# it over. An unrecognised version therefore reads as no map at all, which is the one state
# the box already knows how to repair: `web.py` starts a rebuild and says so meanwhile.
FORMAT = 2

_lock = threading.Lock()
_cache: dict | None = None
_building = False
# A map we have already looked at and refused, by (mtime, size). Without this, a map of the
# wrong format is re-read and re-parsed on every call — eight megabytes of JSON per poll
# for the whole minute a rebuild takes, to reach the same conclusion each time.
_refused: tuple | None = None


def _load() -> dict | None:
    global _refused
    try:
        stat = MAP_FILE.stat()
    except OSError:
        return None
    ident = (stat.st_mtime, stat.st_size)
    if _refused == ident:
        return None
    try:
        data = json.loads(MAP_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "keys" not in data or data.get("format") != FORMAT:
        _refused = ident
        return None
    return data


def load(*, max_age: float = MAX_AGE) -> dict | None:
    """The map as it stands, or None if there is not a usable one."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        data = _cache
    if data is None:
        return None
    if time.time() - float(data.get("built_at") or 0) > max_age:
        data = dict(data)
        data["stale"] = True
    return data


def _seconds(item: "plexmod.PlexItem") -> float:
    """Exact length, never the rounded-for-display minutes.

    `minutes` is quantised to a tenth of a minute. Reconstructing seconds from it snaps
    everything to a six-second grid, which turned a 4.0s bumper into a 6.0s one and so
    made it too long for every gap it was meant to fill.
    """
    if item.seconds is not None:
        return round(item.seconds, 2)
    return round((item.minutes or 0) * 60.0, 2)


def _part_index(items: list) -> tuple[dict[str, tuple], set[str]]:
    """Path tail -> (media_index, seconds, part_index, media_id), movie files only.

    `path_index` answers "which item is this", which is what the safety audit needs and what
    every version of a film answers identically. A player needs the other question — "which
    *file* is this" — and that one has a different answer per version.

    Deliberately supplies values only; it never decides which keys exist. `path_index` deletes
    a key it cannot resolve because there the fallback is stricter — a film with no Plex match
    is rated by its folder name, which defaults to adult. Here there is no such fallback:
    `resolve` returning None makes the app say "Plex cannot identify this file" and play
    nothing, while the box itself plays the right file. So a tail two different files both
    claim loses its *value* and falls back to the item-level answer, and the key survives.
    """
    index: dict[str, tuple] = {}
    claimed: dict[str, str] = {}
    ambiguous: set[str] = set()

    for item in items:
        if item.kind != "movie":
            continue
        for part in item.parts:
            for key in plexmod._suffixes(Path(part.path)):
                staked = claimed.get(key)
                if staked is not None and staked != part.path:
                    ambiguous.add(key)
                    continue
                claimed[key] = part.path
                # setdefault, so if Plex ever lists one file under two Media — an optimised
                # copy pointed back at the original — the lower index wins rather than the
                # last one seen.
                index.setdefault(key, (part.media_index, round(part.seconds or 0.0, 2),
                                       part.part_index, part.media_id))

    for key in ambiguous:
        index.pop(key, None)
    return index, ambiguous


def build() -> dict:
    """Crawl Plex and write the map. Slow on purpose; call it off the request path."""
    client = plexmod.from_config()
    if client is None:
        raise plexmod.PlexError("Plex is not configured on this box.")

    started = time.time()
    items = client.library()
    plexmod.fill_show_paths(client, items)
    index = plexmod.path_index(items)
    episodes = plexmod.episode_index(client, items)

    keys: dict[str, list] = {}
    # Episodes first, then items, so a film never shadows an episode that shares a tail.
    #
    # The duration rides along because Plex already measured it and the ambiance channel
    # needs it: that channel has no schedule to consult, so where it is in a loop can only
    # be arithmetic on the clock, and arithmetic needs lengths.
    for key, episode in episodes.items():
        if episode.rating_key:
            keys[key] = [episode.rating_key, "episode", round(episode.seconds or 0.0, 2),
                         episode.media_index, episode.part_index, episode.media_id]
    per_file, unresolved = _part_index(items)
    # Only the tails that survive `path_index` matter: it has already deleted every tail two
    # different *films* claim, so what is left here is the case that actually costs something
    # — a tail known to be one film, but not which of its versions.
    contested = len(unresolved & set(index))
    for key, item in index.items():
        if not item.rating_key or key in keys:
            continue
        # A folder tail names an item, not a file, so it keeps the item-level answer. Only a
        # tail ending in a real file can say which version it is.
        which = per_file.get(key) if item.kind == "movie" else None
        if which is not None:
            keys[key] = [item.rating_key, "movie", which[1], which[0], which[2], which[3]]
        else:
            keys[key] = [item.rating_key, item.kind or "item", _seconds(item), 0, 0, ""]

    # The keys answer "what is this file called in Plex". `files` answers the opposite
    # question, which a catalogue asks instead: "what does Plex have under this folder".
    # Same crawl, so it costs nothing to write both down.
    files: list[list] = []
    seen_files: set[tuple] = set()
    seen_paths: set[str] = set()
    for episode in episodes.values():
        ident = (episode.path, episode.media_index)
        if episode.path and ident not in seen_files:
            seen_files.add(ident)
            files.append([episode.path, episode.rating_key, "episode",
                          round(episode.seconds or 0.0, 2), episode.media_index])
    for item in items:
        if item.kind != "movie":
            continue
        for part in item.parts:
            # On the path alone, not (path, media_index): a catalogue reads these rows as
            # "what does Plex have here", so one file listed twice is one film scheduled
            # twice. Parts arrive in index order, so the survivor is the lower one.
            if part.path and part.path not in seen_paths:
                seen_paths.add(part.path)
                files.append([part.path, item.rating_key, "movie",
                              round(part.seconds or _seconds(item), 2), part.media_index])

    data = {
        "format": FORMAT,
        "built_at": time.time(),
        "took": round(time.time() - started, 1),
        "server": (plexmod.load_config() or {}).get("url", ""),
        "counts": {"items": len(items), "episodes": len(episodes),
                   "keys": len(keys), "files": len(files),
                   # Tails that name one film but cannot say which version, so they fall
                   # back to the item-level answer. Zero on the library this was written
                   # against; worth seeing if it is ever not.
                   "contested": contested},
        "keys": keys,
        "files": files,
    }
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(MAP_FILE)          # atomic: a reader never sees half a map

    global _cache
    with _lock:
        _cache = data
    return data


def build_in_background() -> bool:
    """Kick off a rebuild unless one is already running. True if this call started it."""
    global _building
    with _lock:
        if _building:
            return False
        _building = True

    def run() -> None:
        global _building
        try:
            data = build()
            print(f"  plexmap: {data['counts']['keys']} keys in {data['took']}s")
        except Exception as exc:  # noqa: BLE001 - a failed crawl must not take the box down
            print(f"  plexmap: build failed: {type(exc).__name__}: {exc}")
        finally:
            with _lock:
                _building = False

    threading.Thread(target=run, daemon=True).start()
    return True


def resolve(path: str | Path, data: dict | None = None) -> dict | None:
    """The Plex item for a path the schedule is about to play, or None.

    Follows the symlink first: the schedule stores `media/st9/shows/...`, which is this box's
    own furniture, and Plex only knows the file it points at on the share.
    """
    data = load() if data is None else data
    if not data:
        return None
    keys = data.get("keys") or {}
    real = Path(os.path.realpath(str(path)))
    for key in plexmod._episode_keys(real) + plexmod._suffixes(real):
        hit = keys.get(key)
        if hit:
            # Read by position with a length guard on each: an entry from an older map is
            # shorter, and a missing field must read as its default rather than raise.
            return {"rating_key": hit[0], "kind": hit[1],
                    "seconds": hit[2] if len(hit) > 2 else 0.0,
                    "media_index": hit[3] if len(hit) > 3 else 0,
                    "part_index": hit[4] if len(hit) > 4 else 0,
                    "media_id": hit[5] if len(hit) > 5 else ""}
    return None
