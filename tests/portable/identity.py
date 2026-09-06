"""Does Plex serve the version the map named? Checked by id, not by a 200."""
import sys
ROOT = sys.argv[1] if len(sys.argv) > 1 else "/home/andrew/8008tub3"
sys.path.insert(0, ROOT)
from tub3 import plexmap, plex as P

client = P.from_config()
SID = "bt-verify-mediaindex"
CASES = [
    ("/mnt/tub3/Media/mshare/Kids Movies/The Gruffalos Child x264/The Gruffalo's Child x264.mkv", "shadowed Gruffalo"),
    ("/mnt/tub3/Media/mshare/Movies/THE_BOURNE_ULTIMATUM/THE_BOURNE_ULTIMATUM.mp4", "shadowed Bourne"),
    ("/mnt/tub3/Media/mshare/Movies/Diehard/Die Hard (1988) WEBDL-1080p.mkv", "primary (control)"),
]
data = plexmap.load(max_age=1e9)
ok = True
for path, label in CASES:
    hit = plexmap.resolve(path, data)
    if not hit:
        print("  %-20s UNRESOLVED %s" % (label, path)); ok = False; continue
    root = client._get("/video/:/transcode/universal/decision", **{
        "path": "/library/metadata/" + hit["rating_key"],
        "mediaIndex": hit["media_index"], "partIndex": hit.get("part_index", 0),
        "protocol": "hls", "directPlay": "0", "directStream": "1",
        "X-Plex-Platform": "Safari", "X-Plex-Client-Identifier": SID, "session": SID})
    served = [(m.get("id"), m.get("duration")) for m in root.iter("Media")]
    got_id, got_ms = served[0] if served else ("", "0")
    agree = got_id == hit.get("media_id")
    ok = ok and agree
    print("  %-20s map idx=%s id=%-6s %8.1fs" % (label, hit["media_index"],
                                                 hit.get("media_id"), hit["seconds"]))
    print("  %-20s plex     id=%-6s %8.1fs   %s" % ("", got_id, int(got_ms or 0) / 1000.0,
                                                    "MATCH" if agree else "*** MISMATCH ***"))
try:
    client._get("/video/:/transcode/universal/stop", session=SID)
except Exception:
    pass
print("\n  every identity confirmed:", ok)
raise SystemExit(0 if ok else 1)
