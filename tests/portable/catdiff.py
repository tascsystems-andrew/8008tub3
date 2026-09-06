"""Would a Plex-backed catalogue give the scheduler the same input?

The scheduler is deterministic given a catalogue: same entries, same durations, same order in,
same blocks out. So comparing the catalogues is the same question as comparing the schedules,
without building anything or going near the live database.

Compared per station, over the folders the lineup actually names.
"""
import sys, json, sqlite3, os
sys.path.insert(0, "/home/andrew/8008tub3")
from pathlib import Path
from tub3 import plexcatalog

DB = "/home/andrew/8008tub3/vendor/FieldStation42/runtime/fs42_fluid.db"
lineup = json.load(open("/home/andrew/8008tub3/lineup.json"))
chans = lineup if isinstance(lineup, list) else lineup.get("channels", lineup)
index = plexcatalog.folder_index()

conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
rows = conn.execute(
    "SELECT station, realpath, duration, content_type FROM catalog_entries "
    "WHERE content_type = 'feature' AND realpath IS NOT NULL").fetchall()
conn.close()

built = {}
for station, realpath, duration, _ct in rows:
    built.setdefault(station, {})[os.path.basename(realpath).lower()] = (realpath, float(duration))

names = {}
for c in chans:
    if c.get("name"):
        names[c["name"]] = c

print("  %-16s %7s %7s %8s %8s %9s" % ("station", "built", "plex", "missing", "extra", "dur diff"))
print("  " + "-" * 64)
T = [0, 0, 0, 0, 0]
for station, entries in sorted(built.items()):
    conf = names.get(station)
    if not conf:
        continue
    plex = {}
    ex_all = conf.get("excludes") or conf.get("exclude") or {}
    for tag, paths in (conf.get("sources") or {}).items():
        ex = ex_all.get(tag) or []
        for folder in paths:
            for rec in plexcatalog.entries_for(folder, index, ex):
                plex[Path(rec["plex_path"]).name.lower()] = rec
    missing = set(entries) - set(plex)
    extra = set(plex) - set(entries)
    diffs = 0
    for name in set(entries) & set(plex):
        ours = entries[name][1]
        theirs = plex[name]["seconds"]
        if theirs and abs(ours - theirs) / max(ours, 1.0) > 0.02:
            diffs += 1
    print("  %-16s %7d %7d %8d %8d %9d" % (
        station[:16], len(entries), len(plex), len(missing), len(extra), diffs))
    T[0] += len(entries); T[1] += len(plex); T[2] += len(missing)
    T[3] += len(extra); T[4] += diffs
print("  " + "-" * 64)
print("  %-16s %7d %7d %8d %8d %9d" % ("TOTAL", *T))
print()
print("  entries the scheduler would lose : %d of %d  (%.1f%%)" % (
    T[2], T[0], 100.0 * T[2] / max(T[0], 1)))
print("  entries it would gain            : %d" % T[3])
print("  durations that would change      : %d" % T[4])
