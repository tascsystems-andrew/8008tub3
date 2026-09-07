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


FLIRC_HELPER = "/usr/local/sbin/tub3-flirc"

# What the dongle currently knows, remembered.
#
# `flirc_util settings` takes three and a half seconds — measured on this box, and it
# is the USB round trip enumerating the key table, not anything here. The settings page
# asks on every load, so without this the page waited three and a half seconds to draw
# a card that almost always says the same thing.
#
# Invalidated by writing, not by a clock: the only thing that changes what the dongle
# knows is this server teaching it something, so a stale read is not possible from
# here. Someone running flirc_util by hand over ssh would go unnoticed until a restart,
# which is a trade worth making for four seconds a page.
_flirc_keys: list | None = None


def flirc(request: dict, timeout: float = 30.0) -> dict:
    """Ask the root helper to do one thing to the infrared dongle.

    Same boundary as `nas`, for the same reason: writing to the dongle's HID interface is
    root's job and this server is an unauthenticated HTTP listener. The helper validates the
    keystroke against its own allowlist rather than trusting the one in the page, because a
    trust boundary that trusts its caller's list is not one.
    """
    import subprocess

    if not Path(FLIRC_HELPER).exists():
        return {"ok": False,
                "error": "The remote helper is not installed — re-run pi_setup.sh."}
    try:
        result = subprocess.run(
            ["sudo", "-n", FLIRC_HELPER],
            input=json.dumps(request), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The dongle took too long to answer."}
    try:
        answer = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "The remote helper returned something unreadable."}
    global _flirc_keys
    if answer.get("ok") and "keys" in answer:
        _flirc_keys = answer["keys"]
    return answer


def flirc_keys(refresh: bool = False) -> dict:
    """The dongle's key list, from memory when we already know it."""
    global _flirc_keys
    if _flirc_keys is not None and not refresh:
        return {"ok": True, "keys": _flirc_keys, "cached": True}
    return flirc({"action": "list"})


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


def health_report(channels: list[dict]) -> dict:
    """The health verdict, or a quiet "unknown" if the module cannot answer.

    A banner that cannot be computed must never become a banner that cries wolf, so every
    failure here degrades to `ok` rather than to a warning nobody can act on.
    """
    try:
        from .health import health  # noqa: PLC0415
        return health(channels)
    except Exception:  # noqa: BLE001 - the settings page must render regardless
        return {"level": "ok", "messages": [], "build": {}, "low_channels": []}


_inventory_cache: tuple[str, float, dict] | None = None
INVENTORY_TTL = 120.0


def _cached_inventory(folder: str) -> dict:
    """Count the commercials, but not four times a minute.

    The folder is on the network share, so counting it costs about a second — measured
    — and /api/status is polled every fifteen. That is a second of NAS traffic every
    fifteen for a number that changes when someone adds a file, which is not often.
    """
    global _inventory_cache
    now = time.time()
    if (_inventory_cache and _inventory_cache[0] == folder
            and now - _inventory_cache[1] < INVENTORY_TTL):
        return _inventory_cache[2]
    counted = ad_inventory(folder)
    _inventory_cache = (folder, now, counted)
    return counted


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
 .banner{display:none;margin:0 0 14px;padding:12px 15px;border-radius:8px;line-height:1.55}
 .banner b{display:block;margin-bottom:5px;font-size:14px}
 .banner div{font-size:13px;opacity:.92}
 .banner.warning{display:block;background:#3a2c12;color:#ffcf7a;border:1px solid #7a5a1e}
 .banner.fault{display:block;background:#3d1b1b;color:#ff9c8f;border:1px solid #803030}
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
 /* The remote, laid out the way it sits in your hand. A list of key names would fit the
    page better and be useless with a remote in the other hand: you look at the thing, not
    at a table. */
 .rgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:280px;margin:14px 0}
 .rgrid .wide{grid-column:span 3}
 .rgrid .two{grid-column:span 2}
 button.rk{background:var(--card);color:var(--fg);border:1px solid var(--line);
   border-radius:8px;padding:12px 4px;font:12px/1.1 ui-monospace,monospace;cursor:pointer;
   box-shadow:none;min-height:42px}
 button.rk:hover{border-color:var(--amber)}
 button.rk.taught{border-color:#3f8f4f;color:#8fd89c}
 button.rk.taught::after{content:" ✓";color:#8fd89c}
 button.rk.busy{border-color:var(--amber);color:var(--amber);animation:pulse 1s infinite}
 @keyframes pulse{50%{opacity:.45}}
 .rpower{background:#7a2320 !important;color:#fff !important}
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

 <div class=banner id=health></div>

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
  <h2>Remote</h2>
  <div class=hint id=remotestate>Checking for the dongle&hellip;</div>
  <div class=rgrid id=rgrid></div>
  <div class=hint>Click a button here, then point your remote at the dongle and press the
   matching button on it. The dongle learns the infrared code and sends a keystroke from
   then on, so the television needs no infrared support of its own.
   <br><br>Two buttons can share a keystroke. Teach both &#9650; and CH&nbsp;+ and either
   will work; the tick appears on both, because the dongle stores one entry per infrared
   code and the television only ever sees the keystroke.
   <br><br><b>SOURCE</b> moves the television on to its next input over HDMI-CEC, one place
   per press &mdash; press it again to keep going round, and again to come back. It is
   driving the set's own INPUT button, so it behaves like that button does. Naming an input
   directly is not possible: the two commands the specification provides for it are both
   ignored by at least one real television. There is no GUIDE button because the guide is a
   channel: press <b>2</b>.
   <br><br>&#9668; and &#9658; currently do the same as channel up and down &mdash; that is
   what a presentation clicker sends for previous and next, and the television has read them
   that way since before it had a remote with a d-pad.
   <br><br>Nothing here can be pressed twice by accident: a code the dongle already knows is
   refused rather than silently moved.</div>
  <div style="margin-top:14px"><button class=ghost id=remoteclear>Forget every button</button></div>
  <div class=hint id=remotemsg></div>
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
let LOWMARK=4;   // replaced by the server's own alert threshold on first poll
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
    // `class=tag${warn}` emitted two class attributes and browsers keep the first, so the
    // amber never rendered — the flag has been silently dead. And the threshold is the
    // server's, not a hardcoded 6: the banner above quoted one number while the row below
    // flagged at another.
    const warn = (left!==undefined && left < LOWMARK) ? ' warn' : '';
    const tag  = left!==undefined ? `<div class="tag${warn}">${left}h left</div>` : '';
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

  const h = s.health || {level:'ok', messages:[]};
  if(h.alert_hours) LOWMARK = h.alert_hours;
  const banner = $('#health');
  banner.className = 'banner' + (h.level === 'ok' ? '' : ' ' + h.level);
  banner.innerHTML = h.level === 'ok' ? '' :
    `<b>${h.level === 'fault' ? 'Needs attention' : 'Worth a look'}</b>` +
    h.messages.map(m => `<div>${m}</div>`).join('');

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

/* The NH314UD, in the order the buttons sit on it. `key` is the keystroke the dongle will
   send; tuner/input.py turns that back into a verb. Buttons with the same key are the same
   keystroke deliberately — the d-pad and the channel rocker do the same job. */
const RKEYS=[
 {l:'POWER',k:'F12',c:'rpower'},{l:'SOURCE',k:'F2'},{l:'INFO',k:'F3'},
 {l:'1',k:'1'},{l:'2',k:'2'},{l:'3',k:'3'},
 {l:'4',k:'4'},{l:'5',k:'5'},{l:'6',k:'6'},
 {l:'7',k:'7'},{l:'8',k:'8'},{l:'9',k:'9'},
 {l:'PREV.CH',k:'F1'},{l:'0',k:'0'},{l:'MUTE',k:'mute'},
 {l:'VOL +',k:'vol_up'},{l:'▲',k:'up'},{l:'CH +',k:'up'},
 {l:'VOL −',k:'vol_down'},{l:'▼',k:'down'},{l:'CH −',k:'down'},
 {l:'◄',k:'left'},{l:'OK',k:'enter'},{l:'►',k:'right'},
 {l:'BACK',k:'escape'},{l:'MENU',k:'tab'},{l:'',k:null},
];
let rtaught=new Set(), rbusy=false;

function rdraw(){
  const g=$('#rgrid'); g.innerHTML='';
  RKEYS.forEach((b,i)=>{
    if(!b.k){ g.appendChild(document.createElement('span')); return; }
    const el=document.createElement('button');
    el.className='rk'+(b.c?' '+b.c:'')+(rtaught.has(b.k)?' taught':'');
    el.textContent=b.l; el.dataset.key=b.k; el.dataset.idx=i;
    el.onclick=()=>rrecord(b,el);
    g.appendChild(el);
  });
}

async function rcall(body){
  const r=await fetch('/api/remote',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return await r.json();
}

async function rrefresh(){
  const r=await rcall({action:'list'});
  if(!r.ok){ $('#remotestate').textContent=r.error||'No dongle found.'; rdraw(); return; }
  rtaught=new Set((r.keys||[]).map(k=>k.key));
  const n=(r.keys||[]).length;
  $('#remotestate').textContent = n
    ? n+' button'+(n===1?'':'s')+' taught.'
    : 'Dongle found, nothing taught yet. Click a button below to start.';
  rdraw();
}

async function rrecord(b,el){
  if(rbusy) return;
  rbusy=true; el.classList.add('busy');
  const was=el.textContent;
  let left=20;
  /* A countdown, not a spinner: the helper is genuinely waiting for a person to press a
     button, and a spinner would say "working" when it means "your turn". */
  el.textContent='press '+b.l+'…';
  const tick=setInterval(()=>{ left--; if(left>0) el.textContent='press '+b.l+'… '+left; },1000);
  const r=await rcall({action:'record',key:b.k});
  clearInterval(tick);
  el.classList.remove('busy'); el.textContent=was; rbusy=false;
  $('#remotemsg').textContent = r.ok ? '' : (r.error||'That did not take.');
  await rrefresh();
}

$('#remoteclear').onclick=async()=>{
  if(!confirm('Forget every button the dongle has learned?')) return;
  const r=await rcall({action:'clear'});
  $('#remotemsg').textContent = r.ok ? 'Dongle cleared.' : (r.error||'Could not clear it.');
  await rrefresh();
};
rrefresh();
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

    _AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac",
                    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
                    ".opus": "audio/opus"}

    def _send_file(self, target: Path) -> None:
        """Serve a file, honouring Range.

        AVFoundation opens a stream by asking for the last few bytes to find the index, then
        seeks back to the front. Without Range every one of those requests is answered with
        the entire file, so a 180 MB track takes minutes to make a sound and the player gives
        up long before. http.server does not implement this, so it is implemented here.
        """
        try:
            size = target.stat().st_size
        except OSError:
            self.send_error(404)
            return

        start, end = 0, size - 1
        partial = False
        raw = self.headers.get("Range", "")
        if raw.startswith("bytes="):
            first, _, last = raw[len("bytes="):].partition("-")
            try:
                if first:
                    start = int(first)
                    if last:
                        end = min(int(last), size - 1)
                else:
                    # A suffix range: the final N bytes, which is how the index is found.
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            else:
                partial = True
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type",
                         self._AUDIO_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a player that changed channel is not a server fault

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
            # One call, shared: `health` is a verdict *about* these channels, and computing
            # it from a second, separately-fetched copy would let the banner disagree with
            # the grid immediately below it.
            channels = channel_status()
            self._json({
                "channels": channels,
                "health": health_report(channels),
                "settings": settings,
                "inventory": _cached_inventory(settings.get("commercials_dir", "")),
                "storage": _scrub(nas({"action": "status"}, timeout=10.0)),
                "ad_loads": {
                    str(k): {"name": v[2], "detail": v[3]} for k, v in AD_LOAD.items()
                },
            })
            return
        if path.startswith("/plex/"):
            # A relay to Plex, for browsers that are not Safari.
            #
            # Safari plays HLS in the media element, which is not subject to CORS, so an iPad
            # talks to Plex directly and none of this runs. Everything else needs hls.js,
            # which fetches over XHR — and Plex answers the preflight by echoing the origin
            # and then serves the GET with `Access-Control-Allow-Origin: https://app.plex.tv`,
            # so the browser drops it. Same origin is the only way round that.
            #
            # Bytes only: no decode, no encode, nothing this box is bad at. It is still a
            # relay of video through an appliance that would rather not, which is exactly why
            # the client that matters does not use it.
            import urllib.error  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            from .plex import load_config  # noqa: PLC0415
            base = (load_config() or {}).get("url", "")
            if not base:
                self.send_error(503, "Plex is not configured")
                return
            target = base.rstrip("/") + "/" + path[len("/plex/"):]
            if urlparse(self.path).query:
                target += "?" + urlparse(self.path).query
            try:
                with urllib.request.urlopen(target, timeout=30) as up:
                    self.send_response(up.status)
                    for header in ("Content-Type", "Content-Length", "Cache-Control"):
                        value = up.headers.get(header)
                        if value:
                            self.send_header(header, value)
                    self.end_headers()
                    while True:
                        chunk = up.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except urllib.error.HTTPError as exc:
                self.send_error(exc.code, exc.reason)
            except Exception:  # noqa: BLE001 - a dropped player is not a server fault
                pass
            return

        if path == "/tv":
            # Aliased, and it matters. A function-local import binds the name for the
            # WHOLE function, so importing it as `PAGE` made the module-level PAGE a
            # local everywhere in do_GET — including the settings page at the top,
            # which then raised UnboundLocalError on every request. The settings page
            # had been returning 500 since this route was added.
            from .tvpage import PAGE as TV_PAGE  # noqa: PLC0415
            body = TV_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/tv/plex":
            # The address only. There is no token to leak — the box does not keep one, and
            # the server answers LAN clients without it.
            from .plex import load_config  # noqa: PLC0415
            self._json({"url": (load_config() or {}).get("url", "")})
            return

        if path == "/api/tv/guide/music":
            # The playlist the guide is playing, so a client that draws its own listings can
            # play the same music over them. Reuses the box's own chooser rather than reading
            # the folder here, so the festive switch happens on both screens on the same day.
            from tuner.guide import music_for  # noqa: PLC0415
            music_dir = load_settings().get("guide_music_dir")
            tracks = music_for(Path(music_dir) if music_dir else None)
            self._json({"tracks": [
                {"index": i, "name": t.name, "url": f"/api/tv/guide/music/{i}"}
                for i, t in enumerate(tracks)
            ]})
            return

        if path.startswith("/api/tv/guide/music/"):
            from tuner.guide import music_for  # noqa: PLC0415
            music_dir = load_settings().get("guide_music_dir")
            tracks = music_for(Path(music_dir) if music_dir else None)
            try:
                track = tracks[int(path.rsplit("/", 1)[1])]
            except (ValueError, IndexError):
                self.send_error(404)
                return
            self._send_file(track)
            return

        if path == "/api/tv/channels":
            from .tvapi import channels  # noqa: PLC0415
            self._json({"channels": channels()})
            return

        if path.startswith("/api/tv/") and path.endswith("/now"):
            from . import plexmap  # noqa: PLC0415
            from .tvapi import now  # noqa: PLC0415
            try:
                number = int(path.split("/")[3])
            except (IndexError, ValueError):
                self.send_error(404)
                return
            # The map takes a minute to crawl, so it is never built on a request. A first
            # call with no map answers honestly with `plex: null` and starts one; by the
            # next poll the answer is complete.
            #
            # Stale counts as a reason. `load` marks a map older than a day and hands it over
            # anyway, and for a long time nothing acted on that — so a map was built once and
            # then kept forever, and every episode Sonarr landed afterwards was a file the app
            # could not identify. The box played it correctly the whole time, which is exactly
            # why nobody noticed.
            mapping = plexmap.load()
            if mapping is None or mapping.get("stale"):
                plexmap.build_in_background()
            self._json(now(number))
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

        if path == "/api/remote":
            # One route, four verbs, forwarded verbatim. The helper is the authority on what
            # may be recorded; repeating its allowlist here would be a second place to get it
            # wrong. `record` blocks while it waits for someone to press a button, which is
            # why the browser shows a countdown rather than a spinner.
            if payload.get("action") == "list":
                self._json(flirc_keys(refresh=bool(payload.get("refresh"))))
                return
            self._json(flirc(payload, timeout=35.0))
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
