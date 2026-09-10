"""The dial as a week: sixteen channels down, twenty-four hours across, seven days.

Read-only, deliberately, and it is the front door rather than a per-channel form because the
questions that make a network are cross-channel questions — what is opposite dinnertime, what
changes on Saturday. A form answers neither.

It also exists to make one particular thing visible. Channel 4 is called SATURDAY AM and has
no dayparts at all: a flat rotation, so it plays Saturday-morning cartoons at three o'clock on
a Tuesday, and has since it was made. Nothing has ever drawn Tuesday, so nobody has ever seen
it. The grid draws Tuesday.
"""

PAGE = """<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The week — BoobTube</title>
<style>
 :root{--ink:#0c0b0a;--dim:#8a8378;--gold:#ffc83c;--purple:#a85cf6;--green:#33ff55;
       --line:#241f1a;--panel:#141210}
 *{box-sizing:border-box}
 body{margin:0;background:var(--ink);color:#eee;
      font:14px/1.45 ui-monospace,Menlo,monospace}
 header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
        align-items:baseline;gap:16px;flex-wrap:wrap}
 h1{margin:0;font-size:20px;color:var(--gold);letter-spacing:.5px}
 .sub{color:var(--dim)}
 nav{display:flex;gap:6px;padding:12px 18px;flex-wrap:wrap}
 nav button{background:#241f1a;color:#eee;border:1px solid #3a322a;border-radius:7px;
            padding:7px 13px;font:inherit;cursor:pointer}
 nav button.on{background:var(--gold);color:#241a06;border-color:var(--gold);font-weight:700}
 nav button .dot{color:var(--purple);font-weight:700}
 nav button.on .dot{color:#241a06}
 .wrap{overflow-x:auto;padding:0 18px 24px}
 table{border-collapse:collapse;min-width:1100px}
 th,td{border:1px solid var(--line);padding:0}
 thead th{position:sticky;top:0;background:var(--panel);color:var(--dim);
          font-weight:400;font-size:12px;padding:6px 4px;text-align:center}
 tbody th{background:var(--panel);text-align:left;padding:8px 10px;white-space:nowrap;
          font-weight:400}
 tbody th b{color:var(--gold);font-weight:700}
 tbody th i{font-style:normal;color:var(--dim);display:block;font-size:11px}
 td{height:34px}
 .slot{height:100%;padding:5px 8px;font-size:12px;white-space:nowrap;overflow:hidden;
       text-overflow:ellipsis;color:#0c0b0a;font-weight:600}
 .empty{background:repeating-linear-gradient(45deg,#151312,#151312 6px,#1b1917 6px,#1b1917 12px)}
 .flat{outline:1px dashed var(--purple);outline-offset:-3px}
 footer{padding:0 18px 40px;color:var(--dim)}
 .audit{margin:18px 18px 0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
 .audit h2{margin:0;padding:9px 12px;background:var(--panel);font-size:13px;
           color:var(--dim);font-weight:400}
 .audit ul{margin:0;padding:8px 12px 12px 28px}
 .audit li{margin:3px 0}
 .t-refused{color:#ff6b6b}.t-mixed{color:var(--gold)}.t-inert{color:var(--dim)}
 .t-unjudged{color:var(--purple)}.t-vetted{color:var(--dim)}
 .ok{color:var(--green)}
</style></head><body>
<header>
  <h1>THE WEEK</h1>
  <span class=sub id=summary>loading…</span>
</header>
<nav id=days></nav>
<div class=wrap><table id=grid></table></div>
<div class=audit id=audit></div>
<footer id=note></footer>
<script>
const DAYS=['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
let DIAL=null, DAY='monday';

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// A stable colour per tag, so the eye can follow a strip across the week without a legend.
function hue(tag){let h=0;for(const c of tag)h=(h*31+c.charCodeAt(0))%360;return h;}

function drawDays(){
  document.getElementById('days').innerHTML = DAYS.map(d=>{
    // A dot marks a day that is not the same as Monday — the whole point of the view.
    const differs = DIAL.channels.some(c=>
      JSON.stringify(c.week[d])!==JSON.stringify(c.week.monday));
    return `<button data-d="${d}" class="${d===DAY?'on':''}">${d.slice(0,3).toUpperCase()}`
         + (differs?' <span class=dot>&bull;</span>':'') + `</button>`;
  }).join('');
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
    DAY=b.dataset.d; drawDays(); drawGrid();});
}

function drawGrid(){
  const head = '<thead><tr><th></th>' +
    Array.from({length:24},(_,h)=>`<th>${String(h).padStart(2,'0')}</th>`).join('') +
    '</tr></thead>';
  const rows = DIAL.channels.map(c=>{
    const cells = Array.from({length:24},(_,h)=>{
      const slot = c.week[DAY][h];
      if(!slot) return '<td class=empty></td>';
      const name = slot.name || slot.tag;
      // A channel with no dayparts runs one tag round the clock. Outlined, because that is
      // the shape that makes a channel called SATURDAY AM play cartoons on Tuesday morning.
      const flat = c.flat ? ' flat' : '';
      return `<td><div class="slot${flat}" title="${esc(slot.tag)}"`
           + ` style="background:hsl(${hue(slot.tag)} 55% 62%)">${esc(name)}</div></td>`;
    }).join('');
    return `<tr><th><b>${c.number}</b> ${esc(c.name)}<i>${esc(c.rating)}`
         + `${c.flat?' · no dayparts':''}</i></th>${cells}</tr>`;
  }).join('');
  document.getElementById('grid').innerHTML = head + '<tbody>' + rows + '</tbody>';
}

function drawAudit(){
  const a = DIAL.audit, tiers = [
    ['refused','REFUSED — not safe to apply',a.problems],
    ['unjudged','UNJUDGED — nothing can rate this',a.unjudged],
    ['mixed','MIXED — can play something above the line before 20:00',a.mixed],
    ['inert','INERT — these exemptions do nothing',a.inert],
  ].filter(t=>t[2] && t[2].length);
  document.getElementById('audit').innerHTML = tiers.length
    ? tiers.map(([k,title,items])=>`<h2 class=t-${k}>${esc(title)}</h2><ul>`
        + items.map(i=>`<li class=t-${k}>${esc(i)}</li>`).join('') + '</ul>').join('')
    : '<h2 class=ok>The safety audit passes with nothing to report.</h2>';
  document.getElementById('note').textContent =
    `${a.vetted} vetted exemption(s). Read-only for now — edits still go through chat.`;
}

fetch('/api/dial/week').then(r=>r.json()).then(d=>{
  if(!d.ok){document.getElementById('summary').textContent = d.error||'could not read the dial';return;}
  DIAL=d;
  document.getElementById('summary').textContent =
    `${d.channels.length} channels · ${d.source}`;
  drawDays(); drawGrid(); drawAudit();
}).catch(e=>{document.getElementById('summary').textContent=String(e);});
</script></body></html>"""
