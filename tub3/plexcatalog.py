"""What Plex has in a folder, in the shape a catalogue wants.

The builder asks the filesystem two questions for every tag on every channel: which files are
under this folder, and how long is each one. The first needs the share mounted; the second is
an ffprobe per file and is the most expensive thing a build does. Plex has already answered
both — it scanned this library and wrote the answers down.

This module answers them from the map instead. Nothing here touches a disk, which is the whole
point: a scheduler that can answer these two questions over HTTP does not need to live on a
machine with the media attached.

Matching is by path *tail*, for the reason `plex.py` gives at length: Plex reports the paths it
sees inside its own container (`/Media/TV/...`) and this box sees them on a CIFS mount
(`/mnt/tub3/Media/mshare/TV/...`). Only the ends agree, and they agree reliably.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from . import plexmap

# How much of a folder's path has to match. Two is enough to separate `TV/Mr. Bean` from
# `Kids TV/Mr. Bean`, and asking for more would fail on the container boundary itself.
FOLDER_DEPTH = 2


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()


def _tail(path: str | Path, depth: int = FOLDER_DEPTH) -> str:
    parts = [p for p in Path(str(path)).parts if p not in ("/", "")]
    return _norm("/".join(parts[-depth:])) if parts else ""


def folder_index(data: dict | None = None) -> dict[str, list[dict]]:
    """Every Plex file, grouped by the tail of every folder above it.

    Every ancestor, not just the immediate parent. Plex files a film as
    `/Media/Movies/<Title>/<file>`, so indexing only the parent keys it under the film's own
    name and a lineup that names `.../Movies` never reaches it — 250 films were invisible for
    exactly that reason. Walking up means a folder matches whether the media sits directly
    inside it, one season down, or one title down.
    """
    data = plexmap.load() if data is None else data
    index: dict[str, list[dict]] = {}
    for entry in (data or {}).get("files") or []:
        path, rating_key, kind, seconds, media_index = (entry + [0] * 5)[:5]
        record = {"plex_path": path, "rating_key": rating_key, "kind": kind,
                  "seconds": float(seconds or 0.0), "media_index": int(media_index or 0),
                  "title": Path(path).stem}
        # Up to four levels up: `/Media/TV/Show/Season 1/file` is the deepest real shape here,
        # and stopping there keeps a library root like `/Media` from collecting everything.
        parent = Path(path).parent
        ancestors = [parent, *list(parent.parents)[:3]]
        for folder in ancestors:
            if str(folder) in ("/", ""):
                continue
            for depth in (1, 2):
                key = _tail(folder, depth)
                if key:
                    index.setdefault(key, []).append(record)
    return index


def entries_for(folder: str | Path, index: dict[str, list[dict]] | None = None) -> list[dict]:
    """The files Plex has under a folder a lineup names.

    Deepest tail first, so `TV/Mr. Bean` is preferred over a bare `Mr. Bean` that would also
    match `Kids TV/Mr. Bean`.
    """
    index = folder_index() if index is None else index
    want = Path(str(folder))
    for depth in (3, 2, 1):
        key = _tail(want, depth)
        if not key:
            continue
        records = index.get(key)
        if records:
            seen: dict[str, dict] = {}
            for record in records:
                seen.setdefault(record["plex_path"] + "#" + str(record["media_index"]), record)
            return sorted(seen.values(), key=lambda r: r["plex_path"])
    return []
