"""Run a build with durations from Plex instead of ffprobe, changing nothing else.

One variable. File discovery, ordering, tags, counts and cooldown all stay exactly as the
control run had them; only the number attached to each entry comes from Plex. If the blocks
come out identical, then the most expensive part of a build — and the part that chains it to a
machine with the media mounted — can be replaced safely.
"""
import sys, json
REPO = "/tmp/parallel/repo"
VENDOR = REPO + "/vendor/FieldStation42"
sys.path.insert(0, REPO)
# `tub3.schedules.main` puts vendor on the path itself, but only once it runs — and the patch
# has to be installed before that. Same module object either way, so the patch survives.
sys.path.insert(0, VENDOR)

from fs42.media_processor import MediaProcessor
from tub3 import plexmap

_original = MediaProcessor.process_one
_map = plexmap.load()
stats = {"plex": 0, "kept_ffprobe": 0, "changed": 0}

def process_one(fname, tag, hints=[], fluid=None, content_type="feature"):
    entry = _original(fname, tag, hints, fluid, content_type)
    if entry is None:
        return entry
    hit = plexmap.resolve(fname, _map)
    seconds = (hit or {}).get("seconds") or 0.0
    if seconds > 0:
        if abs(float(entry.duration) - seconds) > 0.5:
            stats["changed"] += 1
        entry.duration = float(seconds)
        stats["plex"] += 1
    else:
        stats["kept_ffprobe"] += 1
    return entry

MediaProcessor.process_one = process_one

from tub3.schedules import main   # noqa: E402 - after the patch is installed
code = main(sys.argv[1:])
print("  durations from Plex : %d" % stats["plex"])
print("  fell back to ffprobe: %d" % stats["kept_ffprobe"])
print("  values that differed: %d" % stats["changed"])
sys.exit(code or 0)
