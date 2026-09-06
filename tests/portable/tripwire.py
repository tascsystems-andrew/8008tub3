"""Two versions of one film, verified in sequence. The cache must not confuse them.

`plexmap.verify` asks Plex where a version actually sits and corrects the map when it has
moved. It caches, and the cache is the dangerous part: a rating key is exactly the thing that
holds more than one version, so a cache keyed on it alone answers version 0's position for
version 1 too — and then "corrects" a correct entry onto the wrong file, flagging it as
checked. Keyed that way this test fails with all 31 shadowed versions rewritten to index 0,
which is the original bug reinstated by the machinery meant to prevent it.
"""
import json, sys
import os
sys.path.insert(0, os.environ.get("TUB3_ROOT", "/home/andrew/8008tub3"))
from tub3 import plexmap, plex as P

client = P.from_config()
data = plexmap.load(max_age=1e9)

# every multi-version film on the dial, both versions each
films = []
for item in client.library():
    if item.kind == "movie" and len({p.media_index for p in item.parts}) > 1:
        films.append(item)
print("multi-version films:", len(films))

calls = {"n": 0}
real = plexmap._media_indexes
plexmap._media_indexes = lambda rk: (calls.__setitem__("n", calls["n"] + 1),
                                           real(rk))[1]

bad = []
for item in films:
    for part in item.parts:          # version 0 first, then 1, then 2 — the failing order
        hit = {"rating_key": item.rating_key, "kind": "movie", "seconds": part.seconds,
               "media_index": part.media_index, "part_index": part.part_index,
               "media_id": part.media_id}
        out = plexmap.verify(dict(hit), data)
        if out.get("media_index") != part.media_index or out.get("corrected"):
            bad.append((item.title, part.media_id, part.media_index, out.get("media_index")))

print("versions checked  :", sum(len(f.parts) for f in films))
print("plex calls made   :", calls["n"])
print("wrongly rewritten :", len(bad))
for t, mid, want, got in bad[:8]:
    print("   %-34s id=%-6s want index %s, verify said %s" % (t[:34], mid, want, got))

# --- and what it costs when Plex is not answering -------------------------------------------
# Failures cannot be cached the way answers can, so without a pause every poll pays the
# timeout twice — once for what is on and once for what is next — on every tune and every
# programme boundary, for as long as the server is down.
plexmap._media_indexes = real
plexmap._verified = {}
plexmap._verified_for = -1.0
plexmap._verify_pause_until = 0.0
P.load_config = lambda: {"url": "http://10.255.255.1:32400", "token": ""}   # SYN, no reply

import time
down = {"built_at": time.time() - 7200, "keys": {}}
probe = {"rating_key": "12345", "kind": "movie", "seconds": 100.0,
         "media_index": 0, "part_index": 0, "media_id": "999"}
started = time.time()
for _ in range(6):
    plexmap.verify(dict(probe), down)
cost = time.time() - started
budget = plexmap.VERIFY_TIMEOUT * 2
print()
print("plex unreachable  : six verifies cost %.2fs (budget %.1fs, unpaused would be %.0fs)"
      % (cost, budget, 6 * plexmap.VERIFY_TIMEOUT))

raise SystemExit(1 if (bad or cost >= budget) else 0)
