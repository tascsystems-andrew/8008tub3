"""Does Plex list the same files under a folder that the filesystem does?

Durations were the first assumption; membership is the second. A Plex-backed catalogue is
only sound if, given a folder a lineup names, Plex offers the same set of programmes the
builder would have found by walking it.
"""
import sys, json, os
sys.path.insert(0, "/home/andrew/8008tub3")
from pathlib import Path
from tub3 import plexcatalog

VIDEO = {".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".mpg", ".mpeg"}

lineup = json.load(open("/home/andrew/8008tub3/lineup.json"))
chans = lineup if isinstance(lineup, list) else lineup.get("channels", lineup)
index = plexcatalog.folder_index()
print("  folder index keys:", len(index))
print()
print("  %-3s %-15s %-34s %6s %6s %6s" % ("ch", "tag", "folder", "disk", "plex", "missing"))
print("  " + "-" * 86)
tot_disk = tot_plex = tot_missing = 0
worst = []
for c in chans:
    for tag, paths in (c.get("sources") or {}).items():
        for folder in paths:
            disk = set()
            for root, _dirs, names in os.walk(folder):
                for n in names:
                    if Path(n).suffix.lower() in VIDEO and not n.startswith("."):
                        disk.add(n.lower())
            got = plexcatalog.entries_for(folder, index)
            plexnames = {Path(r["plex_path"]).name.lower() for r in got}
            missing = disk - plexnames
            tot_disk += len(disk); tot_plex += len(plexnames); tot_missing += len(missing)
            if missing:
                worst.append((len(missing), folder, sorted(missing)[:2]))
            print("  %-3s %-15s %-34s %6d %6d %6d" % (
                c.get("number"), tag[:15], Path(folder).name[:34],
                len(disk), len(plexnames), len(missing)))
print()
print("  totals: disk=%d  plex=%d  on disk but not in Plex=%d (%.1f%%)" % (
    tot_disk, tot_plex, tot_missing, 100.0 * tot_missing / max(tot_disk, 1)))
if worst:
    print()
    print("  folders Plex is missing files from:")
    for n, folder, examples in sorted(worst, reverse=True)[:6]:
        print("   %3d  %s" % (n, Path(folder).name[:48]))
        for e in examples:
            print("        %s" % e[:66])
