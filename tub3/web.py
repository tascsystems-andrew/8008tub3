"""The settings surface.

Standard library only — no Flask, no FastAPI. Every dependency here becomes a package on
the Pi image, and a settings page is not worth a web framework.

Two surfaces, deliberately different. The on-device menu is a four-button BIOS because it is
driven by a clicker from three metres away. This one is driven by a mouse from a desk, so it
is a normal web page: it can afford text fields, explanations, and the numbers behind a
decision.

The one thing it must never do is lie about the clock. Block lengths are quantised to
10/15/30/60 minutes to stay aligned, so for a given programme length the non-programme time
is *fixed*. A slider promising "10% ads" that cannot be honoured is worse than no slider.
What it actually controls is what fills that time: commercials, or station idents and promos.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "FieldStation42"
DB = VENDOR / "runtime" / "fs42_fluid.db"
SETTINGS = Path(__file__).resolve().parent.parent / "settings.json"
REBUILD_LOG = Path(__file__).resolve().parent.parent / "last-rebuild.log"

DEFAULTS = {
    "ad_load": 3,          # 1..5; 3 is broadcast-realistic
    "cooldown_minutes": 45,
    "fullscreen": False,
    # A list, not a path. A library is rarely one folder, and pointing at a common parent
    # to catch several of them sweeps in everything else that happens to live there — home
    # video, half-finished downloads, the kids' stuff on an adult channel.
    "programs_dirs": [],
    "commercials_dir": "",
}

# Slider position -> (share of the block that is programme, share of non-programme time that
# is commercials rather than station idents). Position 3 is what 90s broadcast actually ran.
AD_LOAD = {
    1: (0.90, 0.30, "Barely any", "Mostly station idents. A break is a short breath."),
    2: (0.82, 0.60, "Light", "Fewer, shorter breaks than broadcast."),
    3: (0.75, 0.90, "Like real TV", "About a quarter of the hour, the way it actually was."),
    4: (0.68, 1.00, "Heavy", "Late-night syndication energy."),
    5: (0.60, 1.00, "Relentless", "You will remember these jingles."),
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS.exists():
        try:
            data.update(json.loads(SETTINGS.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    # Migrate the single programs_dir this used to hold. Someone who set it before the
    # upgrade should not silently lose their channel on the next rebuild.
    legacy = data.pop("programs_dir", "")
    if legacy and not data.get("programs_dirs"):
        data["programs_dirs"] = [legacy]
    data["programs_dirs"] = [str(p) for p in (data.get("programs_dirs") or []) if p]
    return data


def save_settings(data: dict) -> dict:
    current = load_settings()
    for key in DEFAULTS:
        if key in data:
            current[key] = data[key]
    if "programs_dirs" in data:
        # Deduplicate while keeping order: the same folder twice would double every
        # episode's odds of being picked, which reads as "why is it repeating".
        seen, unique = set(), []
        for folder in data["programs_dirs"]:
            folder = str(folder).rstrip("/")
            if folder and folder not in seen:
                seen.add(folder)
                unique.append(folder)
        current["programs_dirs"] = unique
    SETTINGS.write_text(json.dumps(current, indent=2))
    return current


NAS_HELPER = "/usr/local/sbin/tub3-nas"


def nas(request: dict, timeout: float = 60.0) -> dict:
    """Ask the root helper to do one storage thing.

    The settings server does not run as root and should not: it is an unauthenticated HTTP
    listener. Mounting is root's job, so it lives behind `sudo` on exactly one program with
    exactly four verbs.

    The request goes over **stdin**, never argv, because one of its fields is a password and
    argv is readable by every user on the box through `ps`.
    """
    import subprocess

    if not Path(NAS_HELPER).exists():
        return {"ok": False,
                "error": "The storage helper is not installed — re-run pi_setup.sh."}
    try:
        result = subprocess.run(
            ["sudo", "-n", NAS_HELPER],
            input=json.dumps(request), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The server took too long to answer."}
    if result.returncode != 0 and not result.stdout.strip():
        detail = (result.stderr or "").strip().splitlines()[-1:] or ["no detail"]
        return {"ok": False, "error": f"Could not run the storage helper: {detail[0]}"}
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "The storage helper returned something unreadable."}


BROWSE_ROOT = Path("/mnt/tub3")
BROWSE_LIMIT = 300
# Each folder inspected is a round trip to the NAS. Over a site-to-site VPN at ~38 ms that
# is the difference between a browser that feels instant and one that stalls, so the video
# count is only taken for the first N folders and the rest are listed without it.
COUNT_LIMIT = 80


def browse(where: str) -> dict:
    """List folders for the picker.

    Typing a path from memory is a guessing game, and this is a television, not a shell.
    Confined to /mnt/tub3 by resolving first and checking containment — a symlink on the
    share pointing at / would otherwise turn this endpoint into a filesystem browser for
    anyone on the network.
    """
    try:
        base = BROWSE_ROOT.resolve()
    except OSError:
        return {"ok": False, "error": "No network drive is connected yet."}

    target = Path(where) if where else base
    try:
        resolved = target.resolve()
    except (OSError, ValueError):
        return {"ok": False, "error": "That path cannot be read."}

    if not (resolved == base or resolved.is_relative_to(base)):
        return {"ok": False, "error": "That folder is outside the connected drive."}
    if not resolved.is_dir():
        return {"ok": False, "error": "That is not a folder."}

    # Deferred, as elsewhere in this file: tub3.bootstrap pulls in the card renderer, and
    # the settings page should not carry that import cost on every request.
    from .bootstrap import VIDEO_SUFFIXES

    folders, videos = [], 0
    try:
        with os.scandir(resolved) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        folders.append(entry.name)
                    elif Path(entry.name).suffix.lower() in VIDEO_SUFFIXES:
                        videos += 1
                except OSError:
                    continue
    except OSError as exc:
        return {"ok": False, "error": f"Could not read that folder: {exc.strerror or exc}"}

    folders.sort(key=str.lower)
    out = []
    for index, name in enumerate(folders[:BROWSE_LIMIT]):
        entry = {"name": name, "path": str(resolved / name)}
        if index < COUNT_LIMIT:
            entry.update(_shallow_counts(resolved / name))
        out.append(entry)

    return {
        "ok": True,
        "path": str(resolved),
        "parent": None if resolved == base else str(resolved.parent),
        "at_root": resolved == base,
        "videos_here": videos,
        "folders": out,
        "truncated": len(folders) > BROWSE_LIMIT,
    }


def _shallow_counts(folder: Path) -> dict:
    """Videos directly inside, and whether there is anything deeper.

    Deliberately not recursive. A recursive count of a TV library over SMB would take
    minutes; this takes one directory read, and it is enough to answer the only question
    the picker needs to answer — is this the folder, or is it one above it?
    """
    from .bootstrap import VIDEO_SUFFIXES

    videos = subdirs = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        subdirs += 1
                    elif Path(entry.name).suffix.lower() in VIDEO_SUFFIXES:
                        videos += 1
                except OSError:
                    continue
    except OSError:
        return {}
    return {"videos": videos, "subdirs": subdirs}


def _scrub(payload: dict) -> dict:
    """Never let a secret back out over HTTP, even by accident."""
    return {k: v for k, v in payload.items() if k not in ("password", "username")}


def _epoch(text: str) -> float:
    return datetime.strptime(str(text).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()


def channel_status() -> list[dict]:
    if not DB.exists():
        return []
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        # The catalog tables are written minutes before liquid_blocks, so "file exists but
        # schedule table does not" is the normal state throughout a build — which is
        # exactly when someone is refreshing this page to see whether it worked.
        stations = [r[0] for r in conn.execute("SELECT DISTINCT station FROM liquid_blocks")]
        out = []
        now = time.time()
        for station in sorted(stations):
            row = conn.execute(
                "SELECT start_time, end_time, title, plan_json FROM liquid_blocks "
                "WHERE station=? AND start_time <= ? AND end_time > ? LIMIT 1",
                (station, datetime.fromtimestamp(now), datetime.fromtimestamp(now)),
            ).fetchone()
            span = conn.execute(
                "SELECT MIN(start_time), MAX(end_time), COUNT(*) FROM liquid_blocks "
                "WHERE station=?", (station,),
            ).fetchone()

            entry = {"station": station, "on_now": None, "blocks": span[2] if span else 0}
            if span and span[1]:
                remaining = (_epoch(span[1]) - now) / 3600.0
                entry["schedule_hours_left"] = round(max(0.0, remaining), 1)
            if row:
                start, _, title, plan = row
                elapsed = now - _epoch(start)
                running = 0.0
                for item in json.loads(plan):
                    running += float(item.get("duration") or 0)
                    if running >= elapsed:
                        entry["on_now"] = title
                        entry["segment"] = item.get("content_type")
                        break
            out.append(entry)
        return out
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def guide_rows(hours: float = 3.0) -> dict:
    """The listings, as data, for a screen that does not have to crawl.

    The television's guide scrolls because that is what a cable guide *was* — the pace is
    most of the character, and on a sofa you cannot scroll anyway. A browser is the opposite
    situation: there is a pointer, there is room, and the reason anyone opens this page is to
    find something specific without waiting for it to come round again.

    So it reuses the tuner's own row builder and none of its presentation. Same schedule,
    same titles, same window arithmetic — but three hours instead of ninety minutes, because
    a phone held in the hand can carry more than a grid read across a room.
    """
    import time as _time

    from tuner.guide import rows_from_lineup  # noqa: PLC0415
    from tuner.schedule import LiquidChannel, Lineup  # noqa: PLC0415

    from .app import discover_channels  # noqa: PLC0415
    from .lineup import GUIDE_CHANNEL  # noqa: PLC0415

    now = _time.time()
    try:
        stations = [LiquidChannel(number, name, DB, name)
                    for number, name in discover_channels(DB)]
    except Exception:  # noqa: BLE001
        return {"now": now, "rows": []}
    if not stations:
        return {"now": now, "rows": []}

    # The window is widened by asking for more columns than the television draws. `window`
    # rounds to the half hour, which is what makes the columns line up with real clock times
    # rather than with whenever the page happened to load.
    from tuner.guide import window  # noqa: PLC0415

    begin, _ = window(now)
    finish = begin + hours * 3600

    rows = []
    for row in rows_from_lineup(Lineup(stations), now, GUIDE_CHANNEL):
        slots = []
        for slot in row.slots:
            if slot.end <= begin or slot.start >= finish:
                continue
            slots.append({
                "title": slot.title,
                "start": slot.start,
                "end": slot.end,
                "clipped": slot.start < begin,
            })
        rows.append({"number": row.number, "name": row.name, "slots": slots})

    return {"now": now, "begin": begin, "end": finish,
            "rows": sorted(rows, key=lambda r: r["number"])}


def ad_inventory(commercials_dir: str) -> dict:
    """The arithmetic behind 'why do I keep seeing the same ad'.

    A cooldown cannot beat the library. With C distinct spots and one airing every T seconds,
    perfect rotation still returns a spot after C x T — so the honest thing to show is that
    number, not a setting the inventory cannot honour.
    """
    from .bootstrap import VIDEO_SUFFIXES

    root = Path(commercials_dir) if commercials_dir else None
    if not root or not root.exists():
        return {"spots": 0, "known": False}

    count = sum(
        1 for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
    )
    # Roughly one ad every 105s during a break-carrying schedule, measured on a real day.
    repeat_minutes = round(count * 105.0 / 60.0) if count else 0
    return {"spots": count, "known": True, "repeat_minutes": repeat_minutes}


PAGE = """<!doctype html>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>BoobTube</title>
<style>
 /* The house colours, from brand.py. The on-screen menu is phosphor green because it is
    pretending to be a CRT from three metres away; this page is a settings surface on a
    desk, so it wears the mark's own purple and gold instead. Same family, different room.
    --amber is kept as the accent variable name so every existing rule still applies. */
 :root{--bg:#0e0c14;--card:#17141f;--line:#2a2436;--ink:#e8e8ea;--dim:#8b859b;
       --amber:#a85cf6;--gold:#ffc83c}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,system-ui,sans-serif}
 .wrap{max-width:760px;margin:0 auto;padding:32px 20px 64px}
 h1{font:700 22px/1 ui-monospace,monospace;letter-spacing:.10em;color:var(--gold);margin:0 0 4px}
 .sub{color:var(--dim);margin:0;font-size:13px}
 .brand{display:flex;align-items:center;gap:13px;margin:0 0 28px}
 .val{color:var(--gold)}
 button{box-shadow:0 1px 0 rgba(0,0,0,.4)}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:18px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:0 0 14px;font-weight:600}
 label{display:block;font-size:13px;color:var(--dim);margin:14px 0 5px}
 input[type=text]{width:100%;padding:9px 11px;background:#0d0d0d;border:1px solid var(--line);
   border-radius:6px;color:var(--ink);font:13px ui-monospace,monospace}
 input[type=range]{width:100%;accent-color:var(--amber);margin:6px 0}
 .row{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
 .val{color:var(--amber);font-weight:600}
 .hint{font-size:12.5px;color:var(--dim);margin-top:6px}
 /* Was near-black, which read fine on the old green accent and is unreadable on purple. */
 button{background:var(--amber);color:#fff;border:0;border-radius:6px;padding:10px 16px;
   font-weight:650;font-size:13.5px;cursor:pointer}
 button.ghost{background:transparent;color:var(--amber);border:1px solid var(--line)}
 .ch{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
 .ch:last-child{border:0}
 /* The guide grid. Positioned by time rather than laid out in cells, because a programme
    is a span and not a slot — a 95-minute film has to be able to sit across two columns. */
 .gwrap{overflow-x:auto;margin:0 -4px}
 table.guide{border-collapse:collapse;width:100%;min-width:640px}
 table.guide th{position:sticky;top:0;background:var(--line);color:var(--dim);
   font:11px ui-monospace,monospace;text-align:left;padding:5px 8px;font-weight:600}
 table.guide td.chn{width:150px;padding:7px 8px 7px 0;font-size:13px;white-space:nowrap;
   border-top:1px solid var(--line);vertical-align:middle}
 table.guide td.chn b{color:var(--accent);margin-right:7px}
 table.guide td.slots{padding:0;border-top:1px solid var(--line);height:44px}
 .track{position:relative;height:100%}
 .blk{position:absolute;top:4px;bottom:4px;background:var(--line);border-left:2px solid var(--accent);
   padding:5px 7px;font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
 .blk.live{background:#2c3550;color:#fff}
 .nowbar{position:absolute;top:0;bottom:0;width:2px;background:#7ee787;opacity:.85}
 .tag{font:11px ui-monospace,monospace;color:var(--dim)}
 .warn{color:#ffb454}
 .ok{color:var(--amber)}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 @media(max-width:520px){.grid2{grid-template-columns:1fr}}
 select{width:100%;padding:9px 11px;background:#0d0d0d;border:1px solid var(--line);
   border-radius:6px;color:var(--ink);font:13px ui-monospace,monospace}
 input[type=password]{width:100%;padding:9px 11px;background:#0d0d0d;
   border:1px solid var(--line);border-radius:6px;color:var(--ink);
   font:13px ui-monospace,monospace}
 .pick{display:inline-block;margin:3px 5px 0 0;padding:3px 9px;border:1px solid var(--line);
   border-radius:5px;cursor:pointer;font:12px ui-monospace,monospace;color:var(--amber)}
 .pick:hover{border-color:var(--amber)}
 .modal{position:fixed;inset:0;background:rgba(0,0,0,.66);display:flex;
   align-items:center;justify-content:center;padding:20px;z-index:9}
 .modal[hidden]{display:none}
 .sheet{background:var(--card);border:1px solid var(--line);border-radius:12px;
   padding:20px;width:min(620px,100%);max-height:82vh;display:flex;flex-direction:column}
 .crumb{font:12px ui-monospace,monospace;color:var(--dim);word-break:break-all;
   padding-bottom:10px;border-bottom:1px solid var(--line)}
 .picklist{overflow:auto;margin-top:6px;flex:1;min-height:120px}
 .frow{display:flex;justify-content:space-between;align-items:center;gap:10px;
   padding:9px 4px;border-bottom:1px solid var(--line);font-size:14px}
 .frow:last-child{border:0}
 .fname{cursor:pointer;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .fname:hover{color:var(--amber)}
 .chip{display:flex;justify-content:space-between;align-items:center;gap:10px;
   padding:8px 0;border-bottom:1px solid var(--line);font:13px ui-monospace,monospace}
 .chip:last-child{border:0}
 .x{cursor:pointer;color:var(--dim);padding:0 4px}
 .x:hover{color:#ff6b6b}
</style>
<div class=wrap>
 <div class=brand>
  <!-- The mark, same geometry as draw_icon: a coaxial connector's shell, mouth at twelve
       o'clock, with the centre conductor in gold.

       Drawn with stroke-dasharray rather than arc paths. An SVG elliptical arc picks one
       of four possible curves from two flags, and getting them wrong silently yields a
       different arc rather than an error — which is exactly what happened first time.
       A dashed circle has no such ambiguity: circumference is 2*pi*32 = 201.06, so a
       280-degree stroke is 156.4 with a 44.7 gap, rotated so the gap lands at the top. -->
  <svg viewBox="0 0 100 100" width=42 height=42 aria-hidden=true fill=none
       stroke-width=10.5>
   <circle cx=50 cy=54.3 r=32 stroke="#622ca2"
           stroke-dasharray="61.4 139.6" transform="rotate(35 50 54.3)"/>
   <circle cx=50 cy=50.5 r=32 stroke="#9644ec"
           stroke-dasharray="156.4 44.7" transform="rotate(-50 50 50.5)"/>
   <circle cx=50 cy=50.5 r=9.8 fill="#ffc83c" stroke=none/>
  </svg>
  <div>
   <h1>BoobTube</h1>
   <p class=sub id=sub>&nbsp;</p>
  </div>
 </div>

 <div class=card>
  <h2>Guide</h2>
  <div class=gwrap><table class=guide id=guide></table></div>
  <div class=hint id=guidefoot>Loading…</div>
 </div>

 <div class=card>
  <h2>Commercials</h2>
  <div class=row><label style="margin:0">How ad-heavy</label><span class=val id=adlabel></span></div>
  <input type=range min=1 max=5 step=1 id=adload>
  <div class=hint id=adhint></div>
  <div class=hint style="margin-top:12px" id=inventory></div>
 </div>

 <div class=card>
  <h2>Your network drive</h2>
  <div id=mounted></div>
  <div class=grid2>
   <div><label>Server</label><input type=text id=nasserver placeholder="10.0.1.12"></div>
   <div><label>Share</label>
    <select id=nasshare><option value="">— connect to see shares —</option></select></div>
  </div>
  <div class=grid2>
   <div><label>Username</label><input type=text id=nasuser autocomplete=username></div>
   <div><label>Password</label>
    <input type=password id=naspass autocomplete=current-password></div>
  </div>
  <div class=hint>Stored on this box only, readable by root alone, and never sent back to
   this page. Mounted read-only — nothing here can change or delete anything on your NAS.</div>
  <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">
   <button class=ghost id=naslist>Find shares</button>
   <button id=nasmount>Connect</button>
  </div>
  <div class=hint id=nasmsg></div>
 </div>

 <div class=card>
  <h2>Plex</h2>
  <div class=hint id=plexstate>Not connected.</div>
  <div class=grid2>
   <div><label>Plex address</label>
    <input type=text id=plexurl placeholder="http://10.0.1.12:32400"></div>
   <div><label>Plex token <span class=tag>usually not needed</span></label>
    <input type=password id=plextoken autocomplete=off placeholder="leave empty"></div>
  </div>
  <div class=hint>Plex already matched your library against the standard databases, so it
   knows each show's real rating. That is what decides whether something can reach a
   children's channel &mdash; a folder name is a guess, TV-Y is a fact.
   <br><br>Whether a token is needed depends on your Plex server's Network settings, so
   try the address on its own first &mdash; if it works, nothing else to do. If Plex asks
   for one it will say so, and then: open any item in
   Plex &rarr; the <b>&hellip;</b> menu &rarr; <i>Get Info</i> &rarr; <i>View XML</i>, and
   copy <code>X-Plex-Token</code> out of the address bar.</div>
  <div style="margin-top:16px"><button id=plexsave>Connect Plex</button></div>
  <div class=hint id=plexmsg></div>
 </div>

 <div class=card>
  <h2>Shows</h2>
  <div id=proglist></div>
  <button class=ghost id=addshows>Add a folder…</button>
  <div class=hint>Add each folder you want on the channel. Anything you do not add is
   ignored — pointing at one folder above them all would sweep in everything else too.</div>
 </div>

 <div class=card>
  <h2>Commercials</h2>
  <div id=commlist></div>
  <button class=ghost id=setcomm>Choose folder…</button>
  <div class=hint>One folder, holding Kids / Family / Late / Unsorted. Anything in Unsorted
   counts as Late, so it can never reach a kids channel.</div>
 </div>

 <div class=card>
  <div style="margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap">
   <button id=save>Save</button>
   <button class=ghost id=rebuild>Rebuild schedule</button>
  </div>
  <div class=hint id=saved></div>
  <label style="display:flex;align-items:center;gap:9px;margin-top:18px">
   <input type=checkbox id=fullscreen style="width:auto;accent-color:var(--amber)">
   Fill the screen
  </label>
  <div class=hint>Desktop app only. This box always fills the screen.</div>
 </div>
</div>

<!-- The folder picker. A dialog rather than a page, so choosing a folder never loses
     whatever else you had half-typed. -->
<div id=picker class=modal hidden>
 <div class=sheet>
  <div class=row style="margin-bottom:10px">
   <b id=pickwhat>Choose a folder</b>
   <span class=tag id=pickclose style="cursor:pointer">close</span>
  </div>
  <div class=crumb id=pickpath></div>
  <div id=picklist class=picklist>Loading…</div>
  <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
   <button class=ghost id=pickup>Up</button>
   <button id=pickhere>Use this folder</button>
  </div>
  <div class=hint id=pickmsg></div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s);
let LOADS={};
let HOURS={};

const COLS=6;                       // half-hour columns, three hours across
const hhmm=t=>new Date(t*1000).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});

async function drawGuide(){
  let g;
  try{ g = await (await fetch('/api/guide')).json(); }
  catch(e){ $('#guidefoot').textContent='could not read the schedule'; return; }
  if(!g.rows || !g.rows.length){
    $('#guide').innerHTML='';
    $('#guidefoot').textContent='No channels yet — set your folders below, then Rebuild.';
    return;
  }
  const span = g.end - g.begin;
  let html = '<tr><th></th>';
  for(let i=0;i<COLS;i++) html += `<th>${hhmm(g.begin + i*1800)}</th>`;
  html += '</tr>';
  for(const row of g.rows){
    const left = HOURS[row.name];
    const warn = (left!==undefined && left<6) ? ' class=warn' : '';
    const tag  = left!==undefined ? `<div class=tag${warn}>${left}h left</div>` : '';
    html += `<tr><td class=chn><b>${row.number}</b>${row.name}${tag}</td>`;
    html += `<td class=slots colspan=${COLS}><div class=track>`;
    for(const s2 of row.slots){
      const a = Math.max(0,(s2.start-g.begin)/span*100);
      const b = Math.min(100,(s2.end-g.begin)/span*100);
      if(b-a < 0.7) continue;
      const live = (g.now>=s2.start && g.now<s2.end) ? ' live' : '';
      html += `<div class="blk${live}" style="left:${a}%;width:${b-a}%" `
            + `title="${s2.title} · ${hhmm(s2.start)}–${hhmm(s2.end)}">${s2.title}</div>`;
    }
    html += `<div class=nowbar style="left:${(g.now-g.begin)/span*100}%"></div>`;
    html += '</div></td></tr>';
  }
  $('#guide').innerHTML = html;
  $('#guidefoot').textContent = `${g.rows.length} channels · ${hhmm(g.begin)}–${hhmm(g.end)}`;
}

async function refresh(){
  const s=await (await fetch('/api/status')).json();
  LOADS=s.ad_loads;
  // Hours-left is per station and lives on /api/status, so it is carried across to the
  // guide rather than shown twice. It is the one operational number the grid cannot show.
  HOURS = {}; s.channels.forEach(c=>{ HOURS[c.station] = c.schedule_hours_left; });
  drawGuide();

  $('#sub').textContent = `${s.channels.length} channel(s) · ${s.settings.cooldown_minutes} min ad cooldown`;
  $('#adload').value = s.settings.ad_load;
  $('#fullscreen').checked = !!s.settings.fullscreen;
  // Only take the server's copy when there is nothing unsaved to lose. This poll runs
  // every 15s, and without the guard it silently reverted a folder you had just added —
  // which reads as "the box keeps forgetting my folders".
  if(!DIRTY){
    PROGRAMS = (s.settings.programs_dirs||[]).slice();
    COMMERCIALS = s.settings.commercials_dir||'';
    paintFolders();
  }
  paintStorage(s.storage);
  paintLoad();
  const inv=s.inventory;
  $('#inventory').innerHTML = inv.known && inv.spots
    ? `You have <b class=val>${inv.spots}</b> commercials — a spot comes back about every
       <b class=val>${inv.repeat_minutes}</b> minutes. More commercials is the only thing
       that improves that; the cooldown cannot beat the library.`
    : `<span class=warn>No commercials found yet.</span> Point at the folder below.`;
}
function paintLoad(){
  const v=$('#adload').value, d=LOADS[v]||{};
  $('#adlabel').textContent=d.name||''; $('#adhint').textContent=d.detail||'';
}

// --- network drive -------------------------------------------------------------------
function paintStorage(st){
  const box=$('#mounted');
  const live=(st&&st.mounts||[]).filter(m=>m.mounted);
  if(!live.length){ box.innerHTML='<div class=hint>Nothing connected yet.</div>'; return; }
  box.innerHTML=live.map(m=>{
    const top=(m.top_level||[]).map(f=>
      `<span class=pick data-path="${m.mount_point}/${f}">${f}</span>`).join('');
    return `<div class=ch><div><b class=ok>${m.mount_point}</b><br>
      <span class=tag>${m.free_gb} GB free</span></div></div>
      <div style="margin:8px 0 4px">${top}</div>`;
  }).join('');
}

// --- chosen folders ------------------------------------------------------------------
let PROGRAMS=[], COMMERCIALS='';
function paintFolders(){
  $('#proglist').innerHTML = PROGRAMS.length
    ? PROGRAMS.map((p,i)=>`<div class=chip><span>${p}</span>
        <span class=x data-i="${i}" title="Remove">&times;</span></div>`).join('')
    : '<div class=hint>None yet.</div>';
  $('#proglist').querySelectorAll('.x').forEach(el=>el.onclick=()=>{
    PROGRAMS.splice(+el.dataset.i,1); paintFolders(); dirty();
  });
  $('#commlist').innerHTML = COMMERCIALS
    ? `<div class=chip><span>${COMMERCIALS}</span>
        <span class=x id=commx title="Remove">&times;</span></div>`
    : '<div class=hint>None yet.</div>';
  const cx=$('#commx'); if(cx) cx.onclick=()=>{COMMERCIALS=''; paintFolders(); dirty();};
}
let DIRTY=false;
// Folder choices save the moment they change. Making someone press Save after picking a
// folder is a step whose only purpose is to be forgotten — and forgetting it looked
// exactly like the box losing the folder. Rebuild stays explicit: it takes a minute.
async function dirty(){
  DIRTY=true;
  $('#saved').textContent='Saving…';
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({programs_dirs:PROGRAMS,commercials_dir:COMMERCIALS})});
  DIRTY=false;
  $('#saved').innerHTML='Saved. <b class=val>Rebuild schedule</b> to put it on air.';
}

// --- folder picker -------------------------------------------------------------------
let PICKMODE='programs', PICKPATH='', PICKPARENT=null;
async function openPicker(mode){
  PICKMODE=mode;
  $('#pickwhat').textContent = mode==='programs' ? 'Add a shows folder' : 'Choose the commercials folder';
  $('#picker').hidden=false; $('#pickmsg').textContent='';
  await loadPicker('');
}
async function loadPicker(path){
  $('#picklist').textContent='Loading…';
  const r=await (await fetch('/api/browse',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})).json();
  if(!r.ok){ $('#picklist').innerHTML=`<div class="hint warn">${r.error}</div>`; return; }
  PICKPATH=r.path; PICKPARENT=r.parent;
  $('#pickpath').textContent=r.path;
  $('#pickup').disabled=!r.parent;
  $('#pickup').style.opacity=r.parent?1:.4;
  const rows=r.folders.map(f=>{
    // videos/subdirs answer the only question the picker has to answer: is this the
    // folder, or is it one above it?
    let tag='';
    if(f.videos!==undefined)
      tag = f.videos ? `${f.videos} video${f.videos===1?'':'s'}`
                     : (f.subdirs ? `${f.subdirs} folder${f.subdirs===1?'':'s'}` : 'empty');
    return `<div class=frow>
      <span class=fname data-path="${f.path}">${f.name}</span>
      <span class=tag>${tag}</span>
      <span class=pick data-add="${f.path}">add</span></div>`;
  }).join('');
  $('#picklist').innerHTML = rows || '<div class=hint>No folders in here.</div>';
  if(r.videos_here) $('#pickmsg').textContent=`${r.videos_here} video file(s) directly in this folder.`;
  $('#picklist').querySelectorAll('.fname').forEach(el=>
    el.onclick=()=>loadPicker(el.dataset.path));
  $('#picklist').querySelectorAll('[data-add]').forEach(el=>
    el.onclick=()=>choose(el.dataset.add));
}
function choose(path){
  if(PICKMODE==='programs'){
    if(!PROGRAMS.includes(path)) PROGRAMS.push(path);
  } else { COMMERCIALS=path; }
  paintFolders(); dirty();
  // Adding several folders in a row is the common case, so shows keeps the picker open.
  if(PICKMODE==='programs'){ $('#pickmsg').textContent=`Added ${path}`; }
  else { $('#picker').hidden=true; }
}
$('#plexsave').onclick=async()=>{
  $('#plexmsg').textContent='Asking Plex…';
  const r=await (await fetch('/api/plex',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:$('#plexurl').value.trim(),token:$('#plextoken').value})})).json();
  if(!r.ok){ $('#plexmsg').innerHTML=`<span class=warn>${r.error}</span>`; return; }
  $('#plextoken').value='';          // it has done its job
  const parts=Object.entries(r.by_rating||{}).map(([k,v])=>`${v} ${k}`).join(' · ');
  $('#plexmsg').innerHTML=`<span class=ok>Connected.</span> ${r.items} items across
    ${(r.sections||[]).join(', ')} — ${parts}.` +
    (r.unrated?` <span class=warn>${r.unrated} have no rating in Plex and count as
     adult.</span>`:'');
  $('#plexstate').innerHTML='<span class=ok>Connected</span> — ratings come from Plex.';
};
$('#addshows').onclick=()=>openPicker('programs');
$('#setcomm').onclick=()=>openPicker('commercials');
$('#pickclose').onclick=()=>$('#picker').hidden=true;
$('#pickup').onclick=()=>{ if(PICKPARENT) loadPicker(PICKPARENT); };
$('#pickhere').onclick=()=>choose(PICKPATH);
$('#picker').onclick=e=>{ if(e.target.id==='picker') $('#picker').hidden=true; };

function nasBody(){
  return {server:$('#nasserver').value.trim(), share:$('#nasshare').value,
          username:$('#nasuser').value, password:$('#naspass').value};
}
async function nasCall(action,extra){
  const r=await fetch('/api/storage/'+action,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign(nasBody(),extra||{}))});
  return r.json();
}
$('#naslist').onclick=async()=>{
  $('#nasmsg').textContent='Looking…';
  const r=await nasCall('list');
  if(!r.ok){ $('#nasmsg').innerHTML=`<span class=warn>${r.error}</span>`; return; }
  $('#nasshare').innerHTML=r.shares.map(s=>`<option>${s}</option>`).join('');
  $('#nasmsg').textContent=`${r.shares.length} share(s). Pick one and Connect.`;
};
$('#nasmount').onclick=async()=>{
  if(!$('#nasshare').value){ $('#nasmsg').innerHTML=
    '<span class=warn>Pick a share first — Find shares will list them.</span>'; return; }
  $('#nasmsg').textContent='Connecting…';
  const r=await nasCall('mount');
  if(!r.ok){ $('#nasmsg').innerHTML=`<span class=warn>${r.error}</span>`; return; }
  // The password has done its job. Do not leave it sitting in a form field.
  $('#naspass').value='';
  $('#nasmsg').innerHTML=`<span class=ok>Connected at ${r.mount_point}</span>
    <span class=tag>SMB ${r.version} · ${r.free_gb} GB free</span>
    — now pick your Shows and Commercials folders below.`;
  refresh();
};
$('#adload').addEventListener('input',paintLoad);
$('#save').onclick=async()=>{
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ad_load:+$('#adload').value,
      fullscreen:$('#fullscreen').checked,
      programs_dirs:PROGRAMS,
      commercials_dir:COMMERCIALS})});
  $('#saved').textContent='Saved. Rebuild to apply to the schedule.';
  refresh();
};
$('#rebuild').onclick=async()=>{
  $('#saved').textContent='Rebuilding…';
  const r=await (await fetch('/api/rebuild',{method:'POST'})).json();
  $('#saved').textContent=r.message; refresh();
};
refresh(); setInterval(refresh,15000);
</script>
"""


GUIDE_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BoobTube — Guide</title>
<style>
  :root {
    --ink:#0E0C14; --panel:#1a1622; --edge:#2a2336;
    --phosphor:#55FF33; --gold:#FFC83C; --purple:#A85CF6; --dim:#8b8b8b;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--ink); color:#e8e8e8;
         font:15px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }
  header { padding:18px 20px 12px; border-bottom:3px solid var(--purple);
           display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
  h1 { margin:0; font-size:26px; color:var(--gold); letter-spacing:.06em; }
  .date { color:var(--dim); }
  .clock { margin-left:auto; font-size:26px; color:var(--phosphor); }
  .wrap { overflow-x:auto; padding:0 0 28px; }
  table { border-collapse:collapse; min-width:900px; width:100%; }
  th.time { position:sticky; top:0; background:var(--edge); color:var(--gold);
            font-weight:700; text-align:left; padding:8px 10px; font-size:13px;
            border-left:1px solid var(--ink); }
  th.corner { position:sticky; left:0; top:0; z-index:2; background:var(--edge); width:190px; }
  td.ch { position:sticky; left:0; background:var(--panel); padding:10px;
          border-top:1px solid var(--ink); white-space:nowrap; }
  td.ch b { color:var(--gold); font-size:19px; margin-right:8px; }
  td.ch span { color:var(--phosphor); }
  td.grid { padding:0; border-top:1px solid var(--ink); position:relative; height:52px; }
  .bar { position:absolute; top:4px; bottom:4px; background:#241E2A;
         border-left:3px solid var(--purple); padding:6px 9px; overflow:hidden;
         white-space:nowrap; text-overflow:ellipsis; font-size:13px; }
  .bar.on { background:#33294a; }
  .bar .clip { color:var(--dim); }
  .none { color:var(--dim); padding:10px; font-style:italic; }
  .nowline { position:absolute; top:0; bottom:0; width:2px; background:var(--phosphor);
             opacity:.8; z-index:1; }
  footer { color:var(--dim); padding:0 20px 30px; font-size:13px; }
  a { color:var(--phosphor); }
</style>
<header>
  <h1>GUIDE</h1><span class="date" id="date"></span><span class="clock" id="clock"></span>
</header>
<div class="wrap"><table id="grid"></table></div>
<footer id="foot">loading…</footer>
<script>
const COLS = 6;                       // half-hour columns across three hours
function hhmm(t){ const d=new Date(t*1000);
  return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}); }

async function draw(){
  let data;
  try { data = await (await fetch('/api/guide')).json(); }
  catch(e){ document.getElementById('foot').textContent = 'could not reach the box'; return; }

  const {now, begin, end, rows} = data;
  const span = end - begin;
  document.getElementById('clock').textContent = hhmm(now);
  document.getElementById('date').textContent =
    new Date(now*1000).toLocaleDateString([], {weekday:'long', day:'numeric', month:'long'});

  if(!rows.length){
    document.getElementById('grid').innerHTML = '';
    document.getElementById('foot').textContent = 'nothing scheduled yet';
    return;
  }

  let head = '<tr><th class="corner"></th>';
  for(let i=0;i<COLS;i++) head += `<th class="time">${hhmm(begin + i*1800)}</th>`;
  head += '</tr>';

  let body = '';
  for(const row of rows){
    body += `<tr><td class="ch"><b>${row.number}</b><span>${row.name}</span></td>`;
    body += `<td class="grid" colspan="${COLS}"><div style="position:relative;height:100%">`;
    if(!row.slots.length){ body += '<div class="none">Off air</div>'; }
    for(const s of row.slots){
      const left  = Math.max(0, (s.start - begin) / span * 100);
      const right = Math.min(100, (s.end - begin) / span * 100);
      if(right - left < 0.6) continue;              // too thin to carry a word
      const live = (now >= s.start && now < s.end) ? ' on' : '';
      const mark = s.clipped ? '<span class="clip">‹ </span>' : '';
      body += `<div class="bar${live}" style="left:${left}%;width:${right-left}%" `
            + `title="${s.title} · ${hhmm(s.start)}–${hhmm(s.end)}">${mark}${s.title}</div>`;
    }
    const nowPct = (now - begin) / span * 100;
    body += `<div class="nowline" style="left:${nowPct}%"></div>`;
    body += '</div></td></tr>';
  }
  document.getElementById('grid').innerHTML = head + body;
  document.getElementById('foot').textContent =
    `${rows.length} channels · ${hhmm(begin)}–${hhmm(end)} · refreshes every 30s`;
}
draw(); setInterval(draw, 30000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "tub3"

    def log_message(self, *args):  # noqa: A003 - quiet by default
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/guide":
            body = GUIDE_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/guide":
            self._json(guide_rows())
            return
        if path == "/api/status":
            settings = load_settings()
            self._json({
                "channels": channel_status(),
                "settings": settings,
                "inventory": ad_inventory(settings.get("commercials_dir", "")),
                "storage": _scrub(nas({"action": "status"}, timeout=10.0)),
                "ad_loads": {
                    str(k): {"name": v[2], "detail": v[3]} for k, v in AD_LOAD.items()
                },
            })
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/settings":
            self._json({"settings": save_settings(payload)})
            return

        if path == "/api/plex":
            from .plex import Plex, PlexError, classify_rating, load_config, save_config

            url = str(payload.get("url") or "").strip()
            token = str(payload.get("token") or "").strip()
            if not token:
                token = load_config().get("token", "")     # keep the stored one
            if not url:
                self._json({"ok": False, "error": "The server address is needed."})
                return
            # No token demanded: many servers, including one on your own LAN, answer
            # local clients without one. Try as-is and only complain if Plex objects.
            try:
                client = Plex(url, token)
                items = client.library()
            except PlexError as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return

            save_config(url, token)
            by_rating: dict[str, int] = {}
            unrated = 0
            for item in items:
                by_rating[item.rating] = by_rating.get(item.rating, 0) + 1
                if not item.content_rating:
                    unrated += 1
            self._json({"ok": True, "items": len(items), "by_rating": by_rating,
                        "unrated": unrated,
                        "sections": sorted({i.section for i in items if i.section})})
            return

        if path.startswith("/api/storage/"):
            action = path.rsplit("/", 1)[1]
            if action not in ("list", "mount", "unmount"):
                self.send_error(404)
                return
            # The password travels inbound only. It reaches the helper and stops there;
            # _scrub keeps it out of the response even if the helper ever echoed it back.
            result = nas({**payload, "action": action})
            self._json(_scrub(result))
            return

        if path == "/api/browse":
            self._json(browse(str(payload.get("path") or "")))
            return

        if path == "/api/rebuild":
            settings = load_settings()
            programs = settings.get("programs_dirs")
            ads = settings.get("commercials_dir")
            if not programs or not ads:
                self._json({"message": "Add at least one shows folder and "
                                       "a commercials folder first."})
                return
            self._json({"message": "Rebuild started — this takes a minute."})
            threading.Thread(target=_rebuild, args=(settings,), daemon=True).start()
            return

        self.send_error(404)


def _rebuild(settings: dict) -> None:
    """Run bootstrap out of process: it chdirs and upstream calls exit() on bad config."""
    import subprocess
    import sys

    content_share = AD_LOAD[int(settings.get("ad_load", 3))][0]
    repo = Path(__file__).resolve().parent.parent

    # Build with the build interpreter, not this one. The two sides are split on purpose:
    # the tuner and this settings server run on system Python with mpv and the standard
    # library, so a broken build environment cannot stop the television starting, while
    # bootstrap needs moviepy, ffmpeg-python and Pillow from .venv-build.
    #
    # Spawning with sys.executable therefore ran bootstrap under the interpreter that
    # deliberately lacks its dependencies, and it died on `import PIL` while drawing the
    # station idents — after doing all the slow work of walking the library.
    build_python = repo / ".venv-build" / "bin" / "python"
    interpreter = str(build_python) if build_python.exists() else sys.executable

    # --programs is repeatable, one flag per folder.
    programs: list[str] = []
    for folder in settings.get("programs_dirs") or []:
        programs += ["--programs", folder]
    command = [interpreter, "-m", "tub3.bootstrap",
         *programs,
         "--ads", settings["commercials_dir"],
         "--media-root", str(repo / "media"),
         "--channel", "3",
         "--cooldown", str(settings.get("cooldown_minutes", 45)),
         "--days", "1"]
    result = subprocess.run(
        command,
        cwd=repo, env={**os.environ, "TUB3_CONTENT_SHARE": str(content_share)},
        capture_output=True, text=True,
    )
    # Keep the output. This ran with capture_output and no reader, so a bootstrap that
    # failed left the page saying "Rebuild started" forever with nothing to look at.
    try:
        REBUILD_LOG.write_text(
            f"$ {' '.join(command)}\n\n{result.stdout or ''}\n{result.stderr or ''}"
        )
    except OSError:
        pass


def serve(host: str = "0.0.0.0", port: int = 8008) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  8008TUB3 settings on http://localhost:{port}\n")
    server.serve_forever()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="tub3.web")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8008)
    args = ap.parse_args()
    try:
        serve(args.host, args.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
