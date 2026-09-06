"""Does Plex agree with ffprobe about how long these files are?

The portable scheduler stands on this. If Plex's durations match the ones the builder paid to
probe, the most expensive part of a build can be replaced by an HTTP response. If they do not,
blocks drift and the whole idea is unsound — better to know now than after a rewrite.
"""
import sys, sqlite3, collections
sys.path.insert(0, "/home/andrew/8008tub3")
from tub3 import plexmap

DB = "/home/andrew/8008tub3/vendor/FieldStation42/runtime/fs42_fluid.db"
mapping = plexmap.load()
if not mapping:
    raise SystemExit("no plex map")

conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
rows = conn.execute(
    "SELECT path, realpath, duration, content_type FROM catalog_entries "
    "WHERE duration IS NOT NULL AND duration > 0"
).fetchall()
conn.close()

buckets = collections.Counter()
by_type = collections.defaultdict(lambda: [0, 0])
worst = []
unresolved = 0
compared = 0
for path, realpath, duration, ctype in rows:
    hit = plexmap.resolve(realpath or path, mapping)
    if not hit or not hit.get("seconds"):
        unresolved += 1
        continue
    ours, theirs = float(duration), float(hit["seconds"])
    compared += 1
    delta = abs(ours - theirs)
    rel = delta / max(ours, 1.0)
    if delta <= 1.0:      buckets["within 1s"] += 1;   by_type[ctype][0] += 1
    elif delta <= 5.0:    buckets["1-5s"] += 1;        by_type[ctype][0] += 1
    elif rel <= 0.02:     buckets["under 2%"] += 1;    by_type[ctype][0] += 1
    else:
        buckets["OVER 2%"] += 1
        by_type[ctype][1] += 1
        worst.append((delta, ours, theirs, ctype, (realpath or path)))

print("  catalogue rows with a duration:", len(rows))
print("  compared against Plex         :", compared, " (unresolved:", unresolved, ")")
print()
for k in ("within 1s", "1-5s", "under 2%", "OVER 2%"):
    n = buckets[k]
    print("   %-10s %6d  %5.1f%%" % (k, n, 100.0 * n / max(compared, 1)))
print()
print("  by content type (agree / disagree):")
for ct in sorted(by_type):
    ok, bad = by_type[ct]
    print("   %-12s %6d / %-5d" % (ct, ok, bad))
worst.sort(reverse=True)
if worst:
    print()
    print("  worst disagreements:")
    for delta, ours, theirs, ct, path in worst[:8]:
        print("   %8.1fs  ffprobe=%-9.1f plex=%-9.1f %-11s %s" % (
            delta, ours, theirs, ct, path.split("/")[-1][:52]))
