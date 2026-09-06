"""Control build vs Plex-duration build, block for block."""
import sqlite3, json, sys

def blocks(path, station="FAR AFIELD"):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    rows = c.execute(
        "SELECT start_time, end_time, title, plan_json FROM liquid_blocks "
        "WHERE station=? ORDER BY start_time", (station,)).fetchall()
    c.close()
    return rows

a = blocks("/tmp/parallel/runA.db")
b = blocks("/tmp/parallel/runPLEX.db")
print("  control blocks     :", len(a))
print("  plex-duration blocks:", len(b))
print()

same_start = sum(1 for x, y in zip(a, b) if x[0] == y[0])
same_title = sum(1 for x, y in zip(a, b) if x[2] == y[2])
print("  identical start times :", same_start, "of", min(len(a), len(b)))
print("  identical titles      :", same_title, "of", min(len(a), len(b)))

# how far do the plans agree?
plan_same = plan_diff = 0
first = None
for i, (x, y) in enumerate(zip(a, b)):
    px, py = json.loads(x[3]), json.loads(y[3])
    sx = [(e.get("path"), round(float(e.get("duration") or 0), 1)) for e in px]
    sy = [(e.get("path"), round(float(e.get("duration") or 0), 1)) for e in py]
    if sx == sy:
        plan_same += 1
    else:
        plan_diff += 1
        if first is None:
            names_x = [p.split("/")[-1][:34] for p, _ in sx]
            names_y = [p.split("/")[-1][:34] for p, _ in sy]
            first = (i, x[2], len(sx), len(sy),
                     [n for n in names_x if n not in names_y][:3],
                     [n for n in names_y if n not in names_x][:3])
print()
print("  blocks with an identical plan :", plan_same)
print("  blocks whose plan differs     :", plan_diff)
if first:
    i, title, nx, ny, only_a, only_b = first
    print()
    print("  first differing block: #%d  %s" % (i, str(title)[:52]))
    print("    entries: control=%d plex=%d" % (nx, ny))
    if only_a: print("    only in control:", only_a)
    if only_b: print("    only in plex   :", only_b)
