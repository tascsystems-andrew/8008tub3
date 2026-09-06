"""Does the map address the right *version* of a film?

Plex groups alternate versions of one film under a single `ratingKey` — a 1080p rip and a
720p one, both real files on the share, both found by a folder walk. Which of them you get
is the `mediaIndex` parameter, and a map that always writes 0 sends every request to the
first one. The failure is quiet and total: the player fetches a different cut, and the
schedule's offset into it is wrong by the difference in their lengths.

Run before and after a change to `plexmap`. `wrong version` and `wrong duration` should both
be zero; `behind a multi-version item` should not move, because that is a property of the
library rather than of the code.
"""
import os
import sqlite3
import sys

# The tree to test, so a scratch build can be measured without deploying it.
ROOT = os.environ.get("TUB3_ROOT", "/home/andrew/8008tub3")
sys.path.insert(0, ROOT)

from tub3 import plex as P, plexmap        # noqa: E402

DB = "/home/andrew/8008tub3/vendor/FieldStation42/runtime/fs42_fluid.db"


def truth() -> dict[str, list]:
    """Every part of every multi-version item, keyed by basename, from Plex directly.

    Deliberately not from the map — the map is the thing under test.
    """
    client = P.from_config()
    if client is None:
        raise SystemExit("Plex is not configured on this box.")

    parts: dict[str, list] = {}
    items: dict[str, list] = {}
    for section in client.sections():
        root = client._get("/library/sections/%s/all" % section["key"], includeGuids=1)
        for node in root.findall("Video"):
            medias = node.findall("Media")
            if len(medias) < 2:
                continue
            rows = []
            for index, media in enumerate(medias):
                for part in media.findall("Part"):
                    path = part.get("file")
                    if not path:
                        continue
                    millis = part.get("duration") or media.get("duration")
                    rows.append({"rating_key": node.get("ratingKey"),
                                 "media_index": index,
                                 "media_id": media.get("id") or "",
                                 "seconds": (int(millis) / 1000.0) if millis else 0.0,
                                 "path": path})
            items[node.get("ratingKey")] = rows
            for row in rows:
                parts.setdefault(P._norm(os.path.basename(row["path"])), []).append(row)
    return parts, items


def main() -> int:
    parts, multi = truth()
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    catalog = db.execute("SELECT DISTINCT realpath, station FROM catalog_entries "
                         "WHERE realpath IS NOT NULL").fetchall()
    db.close()

    data = plexmap.load(max_age=1e9)
    if not data:
        raise SystemExit("no map to test")

    behind = wrong_version = wrong_duration = wrong_id = 0
    examples: list[tuple] = []
    for real, station in catalog:
        hit = plexmap.resolve(real, data)
        if not hit or hit.get("rating_key") not in multi:
            continue
        behind += 1
        known = parts.get(P._norm(os.path.basename(os.path.realpath(real))))
        if not known:
            continue
        want = known[0]
        bad_index = want["media_index"] != hit.get("media_index", 0)
        bad_length = abs((hit.get("seconds") or 0.0) - want["seconds"]) > 1.0
        # The index is positional and Plex orders versions by resolution, so importing a
        # better copy re-seats index 0 onto a different file and every stored index shifts.
        # The id does not move. An index that still matches while the id does not is exactly
        # what a stale map looks like, and Plex will not report it — a wrong index answers
        # 200 and serves version 0.
        bad_id = bool(want["media_id"]) and hit.get("media_id") not in ("", want["media_id"])
        wrong_version += bad_index
        wrong_duration += bad_length
        wrong_id += bad_id
        if bad_index or bad_length:
            examples.append((station, os.path.basename(real), hit.get("media_index"),
                             want["media_index"], hit.get("seconds") or 0.0,
                             want["seconds"]))

    print("  multi-version items in the library : %d" % len(multi))
    print("  catalogue files                    : %d" % len(catalog))
    print("  behind a multi-version item        : %d" % behind)
    print("  addressed as the WRONG version     : %d" % wrong_version)
    print("  carrying the WRONG duration        : %d" % wrong_duration)
    print("  naming the WRONG Plex media id     : %d" % wrong_id)
    for station, name, got, want, got_s, want_s in examples[:15]:
        print("     %-14s %-44s index %s->%s  %8.1f -> %8.1f"
              % (station, name[:44], got, want, got_s, want_s))
    return 0 if not (wrong_version or wrong_duration or wrong_id) else 1


if __name__ == "__main__":
    raise SystemExit(main())
