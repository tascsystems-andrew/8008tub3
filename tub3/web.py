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

DEFAULTS = {
    "ad_load": 3,          # 1..5; 3 is broadcast-realistic
    "cooldown_minutes": 45,
    "fullscreen": False,
    "programs_dir": "",
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
    return data


def save_settings(data: dict) -> dict:
    current = load_settings()
    for key in DEFAULTS:
        if key in data:
            current[key] = data[key]
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
    finally:
        conn.close()


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
  <h2>On now</h2>
  <div id=channels>Loading…</div>
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
  <h2>Where your stuff is</h2>
  <label>Shows</label><input type=text id=programs placeholder="/mnt/tub3/Media/TV">
  <div class=hint id=browse_programs></div>
  <label>Commercials</label><input type=text id=commercials placeholder="/mnt/tub3/Media/Commercials">
  <div class=hint id=browse_commercials></div>
  <div class=hint>The commercials folder holds Kids / Family / Late / Unsorted. Anything in
   Unsorted is treated as Late, so it can never reach a kids channel.</div>
  <label style="display:flex;align-items:center;gap:9px;margin-top:16px">
   <input type=checkbox id=fullscreen style="width:auto;accent-color:var(--amber)">
   Fill the screen
  </label>
  <div style="margin-top:18px;display:flex;gap:10px">
   <button id=save>Save</button>
   <button class=ghost id=rebuild>Rebuild schedule</button>
  </div>
  <div class=hint id=saved></div>
 </div>
</div>
<script>
const $=s=>document.querySelector(s);
let LOADS={};

async function refresh(){
  const s=await (await fetch('/api/status')).json();
  LOADS=s.ad_loads;
  $('#channels').innerHTML = s.channels.length ? s.channels.map(c=>{
    const on=c.on_now?`${c.on_now} <span class=tag>${c.segment||''}</span>`:'<span class=tag>off air</span>';
    const left=c.schedule_hours_left!==undefined
      ? `<span class="tag ${c.schedule_hours_left<6?'warn':'ok'}">${c.schedule_hours_left}h of schedule left</span>`:'';
    return `<div class=ch><div><b>${c.station}</b><br>${on}</div><div>${left}</div></div>`;
  }).join('') : '<span class=tag>No channels yet — set your folders below, then Rebuild.</span>';

  $('#sub').textContent = `${s.channels.length} channel(s) · ${s.settings.cooldown_minutes} min ad cooldown`;
  $('#adload').value = s.settings.ad_load;
  $('#fullscreen').checked = !!s.settings.fullscreen;
  $('#programs').value = s.settings.programs_dir||'';
  $('#commercials').value = s.settings.commercials_dir||'';
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
  // Clicking a folder fills whichever box was focused last — no typing paths by hand.
  box.querySelectorAll('.pick').forEach(el=>el.onclick=()=>{
    const target=LASTFIELD||'#programs';
    $(target).value=el.dataset.path;
    $('#saved').textContent=`Set ${target==='#programs'?'Shows':'Commercials'} to ${el.dataset.path}. Save to keep it.`;
  });
}
let LASTFIELD='#programs';
['#programs','#commercials'].forEach(id=>$(id).addEventListener('focus',()=>LASTFIELD=id));

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
    body:JSON.stringify({ad_load:+$('#adload').value,fullscreen:$('#fullscreen').checked,programs_dir:$('#programs').value,
      commercials_dir:$('#commercials').value})});
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
            # Which folders are inside a mounted share. Typing a path from memory is a
            # guessing game, and this is a television, not a shell.
            root = Path(str(payload.get("path") or ""))
            base = Path("/mnt/tub3")
            try:
                resolved = root.resolve()
                if not resolved.is_relative_to(base.resolve()) or not resolved.is_dir():
                    self._json({"ok": False, "error": "Not a folder inside a mounted share."})
                    return
                folders = sorted(p.name for p in resolved.iterdir()
                                 if p.is_dir() and not p.name.startswith("."))
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            self._json({"ok": True, "path": str(resolved), "folders": folders[:200]})
            return

        if path == "/api/rebuild":
            settings = load_settings()
            programs = settings.get("programs_dir")
            ads = settings.get("commercials_dir")
            if not programs or not ads:
                self._json({"message": "Set both folders first."})
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
    subprocess.run(
        [sys.executable, "-m", "tub3.bootstrap",
         "--programs", settings["programs_dir"],
         "--ads", settings["commercials_dir"],
         "--media-root", str(repo / "media"),
         "--channel", "3",
         "--cooldown", str(settings.get("cooldown_minutes", 45)),
         "--days", "1"],
        cwd=repo, env={**__import__("os").environ, "TUB3_CONTENT_SHARE": str(content_share)},
        capture_output=True,
    )


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
