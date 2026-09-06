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
    except OSError:
        return None
    except json.JSONDecodeError:
        # A half-written map — the Pi is a television and gets switched off at the wall.
        # Remembered like any other refusal, or it is re-read and re-parsed on every poll
        # until the rebuild lands. The memo is keyed on (mtime, size), so the replacement
        # is picked up the moment it appears.
        _refused = ident
        return None
    if not isinstance(data, dict) or "keys" not in data or data.get("format") != FORMAT:
        _refused = ident
        return None
    # Every rating key in the map belongs to the server that was crawled. Pointed at a
    # different one, they are somebody else's ids: the request either 404s into a black
    # screen or, if that server happens to reuse the number, plays a different film under
    # the right title. The settings page can change the address without touching the map,
    # so the map has to notice by itself.
    server = (plexmod.load_config() or {}).get("url", "")
    if server and data.get("server") and data["server"] != server:
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


def _previous_size() -> int:
    """How many keys the map being replaced had, whatever format it was in.

    Read raw rather than through `_load`, because the point is to compare against whatever is
    on disk — including a map this version would otherwise refuse.
    """
    try:
        return int(json.loads(MAP_FILE.read_text())["counts"]["keys"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


def _guessed_at(items: list, index: dict, per_file: dict) -> int:
    """How many films the map can find but cannot say which version of.

    Counted in *files*, not path tails, because a tail is not the unit anyone cares about: two
    versions in same-named folders under different roots contest their depth-2 tail while both
    still resolve correctly on their depth-3 one, and counting tails calls that a problem. A
    file is only guessed at when `path_index` can place it but none of its own tails survived
    to say which version it is — and then it is served as version 0 with the item's duration,
    which is the original bug, narrowed rather than gone.
    """
    guessed = 0
    for item in items:
        if item.kind != "movie":
            continue
        for part in item.parts:
            tails = plexmod._suffixes(Path(part.path))
            if any(tail in per_file for tail in tails):
                continue
            if any(tail in index for tail in tails):
                guessed += 1
    return guessed


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
    per_file, _unresolved = _part_index(items)
    contested = _guessed_at(items, index, per_file)
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
    # Paths claimed by any row, of either kind. A file Plex holds in both a TV section and a
    # Movies section would otherwise be emitted twice, and a catalogue reading these rows as
    # "what does Plex have here" would schedule it twice.
    seen_paths: set[str] = set()
    for episode in episodes.values():
        ident = (episode.path, episode.media_index)
        if episode.path and ident not in seen_files:
            seen_files.add(ident)
            seen_paths.add(episode.path)
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
                              round(part.seconds, 2), part.media_index])

    data = {
        "format": FORMAT,
        "built_at": time.time(),
        "took": round(time.time() - started, 1),
        "server": (plexmod.load_config() or {}).get("url", ""),
        "counts": {"items": len(items), "episodes": len(episodes),
                   "keys": len(keys), "files": len(files),
                   # Films the map can find but cannot say which version of, so they are
                   # served as version 0 with the item's duration. Zero on the library this
                   # was written against; worth seeing if it is ever not, because nothing
                   # downstream can detect one — a guessed entry carries no media id, and
                   # every identity check treats a missing id as nothing to check.
                   "guessed": contested},
        "keys": keys,
        "files": files,
    }
    # A crawl can succeed and still be wrong. A section whose storage is not mounted yet
    # answers 200 with nothing in it, contributes no items, and raises nothing — so a Pi that
    # reboots alongside its NAS can write a map missing every film. That map is well-formed,
    # so it would be accepted from then on, and the whole library would look like a permanent
    # Plex fault while the box carried on playing the files perfectly.
    before = _previous_size()
    if before and len(keys) < before // 2:
        raise plexmod.PlexError(
            f"Plex answered with {len(keys)} keys where the last crawl found {before}. "
            "Keeping the existing map; this usually means a library was still mounting. "
            "Delete runtime/plexmap.json if the library really did shrink."
        )

    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())  # or the rename can land before the bytes do
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


# How long a Plex lookup may hold up an answer about what is on. The map's own answer is
# already good enough to play; this only makes it righter, so it must never be the reason a
# channel is slow to tune.
VERIFY_TIMEOUT = 4.0

# Don't rebuild a map that was just built. A version Plex no longer has at all would otherwise
# ask for a rebuild, get a map that still cannot place it, and ask again.
REBUILD_MIN_AGE = 300.0

# How long to stop asking after Plex fails to answer. A failure cannot be cached the way an
# answer can — the server will come back, and the question is still worth asking then — but
# without a pause each poll pays the timeout twice, once for what is on and once for what is
# next, on every tune and every programme boundary. Checking is an improvement on an answer
# the map can already give, so when it is not working it gets out of the way.
VERIFY_RETRY = 60.0
_verify_pause_until = 0.0

# One entry per item, holding *every* version's position — never one entry per item holding a
# single position. A rating key is exactly the thing that carries more than one version, which
# is the premise of this whole file: a cache that remembered only "item 5295 is at index 0"
# would answer that for version 1 as well, decide the map's correct index was wrong, and
# rewrite it to the other version — the check producing the very fault it exists to catch, and
# stamping it `corrected` on the way out. Holding the whole set costs one request either way.
_verified: dict[str, dict[str, int]] = {}
_verified_for: float = -1.0


def _media_indexes(rating_key: str) -> dict[str, int] | None:
    """Where Plex keeps each of that item's versions right now, by media id.

    None means the server could not be asked, which is not the same as an answer: treating an
    unreachable Plex as an out-of-date map would rebuild the whole thing every time it went
    out for lunch. An id simply missing from the returned dict is a real answer — that version
    is gone.
    """
    client = plexmod.from_config()
    if client is None:
        return None
    try:
        root = client._get("/library/metadata/" + str(rating_key), timeout=VERIFY_TIMEOUT)
    except Exception:      # noqa: BLE001 - an unreachable server must not fail the answer
        return None
    return {media.get("id"): index
            for index, media in enumerate(root.iter("Media")) if media.get("id")}


def verify(hit: dict | None, data: dict | None = None) -> dict | None:
    """Is that version still where the map says it is? Correct it now if not.

    `mediaIndex` is positional and Plex orders a film's versions by resolution, so importing a
    better copy re-seats index 0 onto a different file and shifts every index above it. The
    map is rebuilt daily at most, and Plex does not object in the meantime: a wrong index
    answers 200 and serves whatever is at that position. The Media id does not move, which is
    the whole reason it is stored.

    Costs one Plex request per item per map build, cached, and only for entries that carry an
    id. It never raises and never turns a hit into a miss — if Plex cannot be reached the
    map's own answer stands, because the wrong version is still a picture and None is not.
    """
    want = (hit or {}).get("media_id")
    rating_key = (hit or {}).get("rating_key")
    if not want or not rating_key:
        return hit

    data = load() if data is None else data
    built = float((data or {}).get("built_at") or 0)

    global _verified, _verified_for, _verify_pause_until
    now = time.time()
    with _lock:
        if built != _verified_for:
            _verified = {}
            _verified_for = built
        places = _verified.get(rating_key)
        paused = now < _verify_pause_until

    if places is None:
        if paused:
            return hit                      # Plex is not answering; the map's word stands
        places = _media_indexes(rating_key)
        if places is None:
            with _lock:
                _verify_pause_until = time.time() + VERIFY_RETRY
            return hit                      # not cached as an answer: only the asking pauses
        with _lock:
            _verified[rating_key] = places

    found = places.get(want, -1)
    if found == hit.get("media_index", 0):
        return hit

    # The map is out of date about this item. Rebuild it, but answer this request now — the
    # rebuild takes over a minute and the viewer is waiting on a picture.
    if time.time() - built > REBUILD_MIN_AGE:
        build_in_background()
    if found < 0:
        return hit                          # Plex no longer has it; nothing better to offer
    corrected = dict(hit)
    corrected["media_index"] = found
    corrected["corrected"] = True
    return corrected


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
