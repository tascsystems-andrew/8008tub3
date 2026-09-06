"""A watchable channel in a browser, for the iPad.

Not a player in any real sense — Safari plays HLS natively, so the `<video>` element is the
player. This is the *tuner*: it asks the box what is on, asks Plex for that item at that
offset, and puts the answer on screen. Everything hard is already done by something else,
which is the whole reason this can be one file.

Deliberately no Plex token. The server answers LAN clients unauthenticated, so a page opened
at home or down the VPN needs no credential — nothing to store in a browser, nothing to leak,
nothing to rotate. If that ever changes this page stops working and asks for one; it does not
quietly start holding a secret.
"""

PAGE = """<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes>
<title>BoobTube</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js"></script>
<style>
 :root{--ink:#0c0b0a;--dim:#8a8378;--gold:#ffc83c;--purple:#a85cf6}
 *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
 body{margin:0;background:var(--ink);color:#eee;font:15px/1.45 ui-monospace,Menlo,monospace;
      height:100dvh;display:flex;flex-direction:column;overflow:hidden}
 #screen{position:relative;flex:1;background:#000;min-height:0}
 video{width:100%;height:100%;object-fit:contain;background:#000}
 #bug{position:absolute;top:14px;right:16px;text-align:right;pointer-events:none;
      text-shadow:0 2px 6px #000;transition:opacity .5s}
 #bug b{display:block;font-size:34px;color:var(--gold);line-height:1}
 #bug span{font-size:13px;color:var(--purple)}
 #now{position:absolute;left:16px;bottom:14px;right:16px;pointer-events:none;
      text-shadow:0 2px 6px #000;font-size:14px}
 #now i{font-style:normal;color:var(--dim)}
 #tuning{position:absolute;inset:0;display:none;place-items:center;background:#000c;
         font-size:20px;color:var(--gold)}
 #dial{display:flex;gap:6px;padding:10px;overflow-x:auto;background:#141210;
       border-top:1px solid #241f1a;flex:0 0 auto}
 button{background:#241f1a;color:#eee;border:1px solid #3a322a;border-radius:8px;
        padding:9px 13px;font:inherit;font-size:14px;white-space:nowrap;cursor:pointer}
 button.on{background:var(--gold);color:#241a06;border-color:var(--gold);font-weight:700}
 button b{color:var(--gold)} button.on b{color:#241a06}
 #msg{padding:8px 12px;color:var(--dim);font-size:12.5px;background:#141210}
</style></head><body>
<div id=screen>
  <video id=v playsinline controls></video>
  <div id=bug><b id=bugnum></b><span id=bugname></span></div>
  <div id=now></div>
  <div id=tuning>tuning…</div>
</div>
<div id=dial></div>
<div id=msg>starting…</div>
<script>
const $=s=>document.querySelector(s);
let PLEX=null, CH=null, CHANS=[], timer=null, current=null, hls=null;
const SID = 'bt-' + Math.random().toString(36).slice(2,10);

// Safari plays HLS in the element and does it in hardware, which on an iPad is the whole
// point. Everything else claims `canPlayType: "maybe"` and then fails to demux — Chrome says
// maybe and raises DEMUXER_ERROR_COULD_NOT_PARSE — so anything not Safari gets hls.js.
const SAFARI = /Safari/.test(navigator.userAgent) && !/Chrome|Chromium|Edg/.test(navigator.userAgent);

function play(url){
  const v = $('#v');
  if(hls){ hls.destroy(); hls = null; }
  if(!SAFARI && window.Hls && Hls.isSupported()){
    hls = new Hls({lowLatencyMode:false, enableWorker:true});
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=> v.play().catch(()=>{}));
    hls.on(Hls.Events.ERROR, (_,d)=>{ if(d.fatal) $('#msg').textContent = 'stream error: '+d.details; });
  } else {
    v.src = url;
    v.play().catch(()=>{ $('#msg').textContent = 'tap the picture to start playback'; });
  }
}

function plexParams(ratingKey, offset){
  return new URLSearchParams({
    path:'/library/metadata/'+ratingKey, mediaIndex:'0', partIndex:'0',
    protocol:'hls', offset:String(Math.max(0,Math.floor(offset))), fastSeek:'1',
    directPlay:'0', directStream:'1', videoQuality:'100', maxVideoBitrate:'20000',
    location:'lan', autoAdjustQuality:'0',
    'X-Plex-Platform':'Safari', 'X-Plex-Client-Identifier':SID, session:SID
  });
}

// Plex will not start a stream it has not agreed to first. `start.m3u8` on a cold session
// answers 400; the same URL after a `decision` call on that session answers 200. Every real
// Plex client does this handshake, and skipping it looked like a malformed request.
//
// The decision always goes through the box even for Safari: it is an XHR either way, and
// Plex answers the preflight by echoing the origin and then serves the GET with
// `Access-Control-Allow-Origin: https://app.plex.tv`.
async function agree(params){
  try { await fetch('/plex/video/:/transcode/universal/decision?' + params.toString()); }
  catch(e){ /* the start below will report anything that actually matters */ }
}

function streamUrl(params){
  // Safari plays from Plex directly — a media element is not subject to CORS and the decode
  // is hardware. Everyone else needs hls.js, which fetches, so it goes through the box.
  const base = SAFARI ? PLEX : '/plex';
  return base + '/video/:/transcode/universal/start.m3u8?' + params.toString();
}

async function tune(ch){
  CH = ch;
  paint();
  $('#tuning').style.display='grid';
  clearTimeout(timer);
  await refresh(true);
}

async function refresh(force){
  let d;
  try { d = await (await fetch('/api/tv/'+CH+'/now')).json(); }
  catch(e){ $('#msg').textContent='the box is not answering'; timer=setTimeout(()=>refresh(true),5000); return; }

  if(d.off_air || !d.now){
    $('#msg').textContent = d.error || 'off air';
    $('#now').textContent = '';
    $('#tuning').style.display='none';
    timer = setTimeout(()=>refresh(true), 15000);
    return;
  }
  const n = d.now;
  $('#bugnum').textContent = d.channel;
  $('#bugname').textContent = d.station;
  const label = (n.title||'').replace(/^[a-z0-9]+__/,'').replace(/\\./g,' ');
  $('#now').innerHTML = label + (n.content_type!=='feature' ? ' <i>· '+n.content_type+'</i>' : '');

  // Only re-point the player when the thing being played actually changed. Re-issuing the
  // same stream on every poll would restart it every few seconds.
  const key = n.plex ? n.plex.rating_key+'@'+Math.floor(n.offset_seconds/5) : null;
  if(!n.plex){
    $('#msg').textContent = 'Plex cannot identify this file' + (d.map ? '' : ' (map still building)');
  } else if(force || current !== n.plex.rating_key){
    current = n.plex.rating_key;
    const params = plexParams(n.plex.rating_key, n.offset_seconds);
    await agree(params);
    play(streamUrl(params));
    $('#msg').textContent = 'ch '+d.channel+' · '+n.plex.kind+' '+n.plex.rating_key+
                            ' · from '+Math.round(n.offset_seconds)+'s';
  }
  $('#tuning').style.display='none';

  // Come back when this item ends, and a beat after, so the next one has been written.
  const wait = Math.max(2, (n.remaining_seconds||0)) * 1000 + 900;
  clearTimeout(timer);
  timer = setTimeout(()=>refresh(true), Math.min(wait, 600000));
}

function paint(){
  $('#dial').innerHTML = CHANS.map(c =>
    `<button class="${c.channel===CH?'on':''}" onclick="tune(${c.channel})">`+
    `<b>${c.channel}</b> ${c.station}</button>`).join('');
}

(async function(){
  try{
    const info = await (await fetch('/api/tv/plex')).json();
    PLEX = info.url;
    if(!PLEX){ $('#msg').textContent='Plex is not configured on the box'; return; }
    CHANS = (await (await fetch('/api/tv/channels')).json()).channels || [];
  }catch(e){ $('#msg').textContent='cannot reach the box'; return; }
  if(!CHANS.length){ $('#msg').textContent='no channels'; return; }
  paint();
  tune(CHANS[0].channel);
})();
</script></body></html>
"""
