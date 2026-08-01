"""Web UI - single page, no build step. Hero-countdown design."""
import json
import time

from flask import Flask, jsonify, request

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>BatiBot</title>
<style>
 :root{--bg:#131316;--card:#1c1c20;--line:#2a2a30;--ink:#e8e8ea;--mut:#9a9a9e;--dim:#6b6b70;--blue:#2f7fd0;--green:#34c98a;--red:#d8455a}
 *{box-sizing:border-box}
 body{background:var(--bg);color:var(--ink);font-family:'Segoe UI',system-ui,Arial,sans-serif;margin:0;padding:0 20px 40px}
 .wrap{max-width:560px;margin:0 auto}
 .hero{text-align:center;padding:44px 0 26px}
 .eyebrow{font-size:11px;letter-spacing:2px;color:var(--mut);text-transform:uppercase}
 .timer{font-size:64px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.05;margin:6px 0 2px;letter-spacing:-1px}
 .sub{font-size:13px;color:var(--mut);margin-bottom:22px;min-height:18px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 1.6s infinite}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
 .bigbtn{display:inline-block;border:0;border-radius:10px;padding:12px 56px;font-size:15px;font-weight:600;cursor:pointer;color:#fff;background:var(--blue);transition:transform .06s}
 .bigbtn:active{transform:scale(.97)}
 .bigbtn.stop{background:var(--red)}
 details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:10px;overflow:hidden}
 summary{cursor:pointer;list-style:none;padding:14px 18px;font-size:14px;font-weight:600;color:var(--ink);display:flex;align-items:center;justify-content:space-between;user-select:none}
 summary::-webkit-details-marker{display:none}
 summary:after{content:'\\2335';color:var(--dim);font-size:13px;transition:transform .15s}
 details[open] summary:after{transform:rotate(180deg)}
 .body{padding:2px 18px 16px;border-top:1px solid var(--line)}
 label{display:block;font-size:12px;color:var(--mut);margin:12px 0 5px}
 input[type=text],input[type=number]{width:100%;background:#232328;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-size:14px;outline:none;color-scheme:dark}
 input[type=text]:focus,input[type=number]:focus{border-color:var(--blue)}
 .row{display:flex;gap:10px}.row>div{flex:1}
 .toggle{display:flex;align-items:flex-start;gap:9px;margin-top:12px;font-size:13px;color:#c9c9ce;line-height:1.45}
 .toggle input{margin-top:2px;accent-color:var(--blue)}
 .mini{background:#26303c;color:#9cc2e8;border:0;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}
 .chip{display:inline-block;background:#243141;color:#a9c9ea;border-radius:14px;padding:4px 11px;margin:3px 3px 0 0;font-size:12.5px}
 .chip.blk{background:#3a2028;color:#e6a4b0}
 .chip a{color:#d8455a;text-decoration:none;margin-left:6px;font-weight:600}
 .muted{color:var(--dim);font-size:12px}
 .actions{display:flex;gap:8px;margin-top:14px}
 .ghost{background:transparent;border:1px solid var(--line);color:#b8b8bd;border-radius:8px;padding:8px 18px;font-size:13px;font-weight:600;cursor:pointer}
 .histsum{font-size:12px;color:var(--mut);padding:8px 0 10px}
 .histwrap{overflow-x:auto;overflow-y:auto;max-height:470px}
 table.hist thead th{position:sticky;top:0;background:var(--card);z-index:1}
 table.hist{width:100%;border-collapse:collapse;font-size:12.5px}
 table.hist th{text-align:left;font-weight:600;color:var(--mut);font-size:11px;
   text-transform:uppercase;letter-spacing:.4px;padding:0 8px 6px 0;white-space:nowrap}
 table.hist td{padding:7px 8px 7px 0;border-top:1px solid var(--line);vertical-align:top}
 table.hist td.num{text-align:right;white-space:nowrap}
 table.hist td.dim{color:var(--dim)}
 table.hist td.mono{font-family:Consolas,monospace;font-size:12px;color:#c2c8d0}
 table.hist td.nowrap{white-space:nowrap}
 .grade{display:inline-block;background:#243141;color:#9cc2e8;border-radius:6px;
   padding:1px 7px;font-weight:700}
 .sparks{color:#8fa8c4;font-size:11px;margin-top:3px;max-width:260px;
   overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 pre{background:#0c0c0e;color:#c9d2dd;border-radius:8px;padding:12px;height:240px;overflow-y:auto;font-size:12px;white-space:pre-wrap;margin:12px 0 0}
 .foot{text-align:center;font-size:11.5px;color:var(--dim);margin-top:18px}
</style></head><body>
<div class="wrap">

<div class="hero">
 <div class="eyebrow" id="eyebrow">Ready</div>
 <div class="timer" id="timer">--:--</div>
 <div class="sub" id="sub">press start to begin looping careers</div>
 <button class="bigbtn" id="mainbtn" onclick="mainAction()">Start</button>
 <div id="ticker" onclick="toggleMini()" title="Click to expand recent log"
  style="cursor:pointer;font-family:Consolas,monospace;font-size:11.5px;color:#b3b3b8;margin-top:16px;padding:5px 10px;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
  <span id="tickertext">waiting for the bot&hellip;</span> <span id="caret" style="color:#c6c6cb">&#9662;</span>
 </div>
 <div id="mini" style="display:none;background:#101113;border-radius:8px;padding:10px 12px;margin-top:6px;text-align:left;font-family:Consolas,monospace;font-size:11px;line-height:1.65;color:#9aa6b2;max-height:170px;overflow-y:auto"></div>
</div>

<details>
 <summary>Career setup</summary>
 <div class="body">
  <label>Borrow support card (typing filters the list)</label>
  <input type="text" id="borrow_name" list="carddata" placeholder="start typing a card title or uma name">
  <datalist id="carddata"></datalist>
  <label>Backup borrow card (used when the first has no lender)</label>
  <input type="text" id="borrow_backup" list="carddata" placeholder="optional second choice">
  <div class="row">
   <div><label>Max careers (0 = until TP runs out)</label><input type="number" id="max_careers" min="0"></div>
  </div>
  <label>Training Focus (re-selected every career; game resets it)</label>
  <select id="it_focus" style="width:100%;background:#232328;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-size:14px">
   <option value="">Leave as is</option>
   <option value="Balanced">Balanced</option>
   <option value="Stamina">Stamina</option>
   <option value="Sprint">Sprint</option>
  </select>
  <div class="toggle"><input type="checkbox" id="load_agenda"><span>Load my FIRST saved agenda before each career (save your schedule in-game once: Agenda &gt; My Agendas &gt; Save Here on the top slot)</span></div>
  <div class="toggle"><input type="checkbox" id="auto_reroll"><span>Auto-reroll sparks once per career (costs 30 TP; keeps the starrier set)</span></div>
  <div class="toggle"><input type="checkbox" id="recover_tp"><span>Refill TP when it runs out &mdash; uses TP Drinks first, then <b>buys with carats</b> (Max). Off = bot stops when TP is empty.</span></div>
  <div class="toggle" style="margin-left:24px"><input type="checkbox" id="recover_tp_carats_only"><span>Carats only &mdash; never use TP Drinks, always buy TP to max with carats</span></div>
 </div>
</details>

<details>
 <summary>Skills</summary>
 <div class="body">
  <label>Buy these first (typing filters the list)</label>
  <input type="text" id="skillpick" list="skilldata" placeholder="skill name, then Enter or Add">
  <datalist id="skilldata"></datalist>
  <button class="mini" onclick="addSkill()">Add</button>
  <div id="skilllist" style="margin-top:8px"></div>
  <label>Never buy these</label>
  <input type="text" id="blockpick" list="skilldata" placeholder="skill name, then Enter or Add">
  <button class="mini" onclick="addBlocked()">Add</button>
  <div id="blockedlist" style="margin-top:8px"></div>
  <div class="toggle"><input type="checkbox" id="smart_skills" checked><span>Smart skill buying &mdash; reads every price and computes the best rating-per-SP basket, listed skills reserved first (recommended)</span></div>
  <div class="toggle"><input type="checkbox" id="spend_all_sp" checked><span>Spend ALL leftover SP (fallback mode if smart buying can't read the screen)</span></div>
 </div>
</details>

<details>
 <summary>Connection</summary>
 <div class="body">
  <div class="row">
   <div><label>ADB address (MuMu default 127.0.0.1:16384)</label><input type="text" id="adb_address"></div>
   <div><label>ADB executable (blank = "adb" on PATH)</label><input type="text" id="adb_path"></div>
  </div>
 </div>
</details>

<details>
 <summary>History</summary>
 <div class="body">
  <div id="history" style="font-size:13px; padding-top:10px;"><span class="muted">no careers finished yet</span></div>
 </div>
</details>

<details>
 <summary>Log</summary>
 <div class="body">
  <pre id="log"></pre>
  <button class="mini" onclick="clearLog()" style="margin-top:10px">Clear log</button>
 </div>
</details>

<div class="actions" style="justify-content:center">
 <button class="ghost" id="savebtn" onclick="saveClicked()">Save settings</button>
</div>
<div class="foot">BatiBot &middot; set Training Focus / Agenda / Prioritized Skills in the game once &mdash; the bot only presses Start</div>

</div>
<script>
let ALL_SKILLS=[], chosenSkills=[], blockedSkills=[], ALL_CARDS=[];
let running=false, srvRemaining=0, srvAt=0, srvState='idle', careers=0;

async function loadSkills(){
 try{ const r=await fetch('/api/skills'); ALL_SKILLS=await r.json(); }catch(e){ ALL_SKILLS=[]; }
 document.getElementById('skilldata').innerHTML = ALL_SKILLS.map(s=>`<option value="${s.replace(/"/g,'&quot;')}">`).join('');
 try{ const r2=await fetch('/api/cards'); ALL_CARDS=await r2.json(); }catch(e){ ALL_CARDS=[]; }
 document.getElementById('carddata').innerHTML = ALL_CARDS.map(c=>`<option value="${c.name.replace(/"/g,'&quot;')}">${c.desc}</option>`).join('');
}
function renderSkills(){
 document.getElementById('skilllist').innerHTML = chosenSkills.map((s,i)=>
  `<span class="chip">${s}<a href="#" onclick="chosenSkills.splice(${i},1);renderSkills();return false">&#10005;</a></span>`).join('') ||
  '<span class="muted">none - bot will rely on smart buying alone</span>';
}
function renderBlocked(){
 document.getElementById('blockedlist').innerHTML = blockedSkills.map((s,i)=>
  `<span class="chip blk">${s}<a href="#" onclick="blockedSkills.splice(${i},1);renderBlocked();return false">&#10005;</a></span>`).join('') ||
  '<span class="muted">none blocked</span>';
}
function canonSkill(v){
 if(ALL_SKILLS.includes(v)) return v;
 const norm = s => s.toUpperCase().replace(/[^A-Z0-9]/g,'');
 const nv = norm(v);
 if(!nv) return null;
 const hits = ALL_SKILLS.filter(s => norm(s) === nv);
 if(hits.length === 1) return hits[0];
 const partial = ALL_SKILLS.filter(s => norm(s).includes(nv));
 if(partial.length === 1) return partial[0];
 if(partial.length > 1){ alert('"'+v+'" matches several skills: '+partial.slice(0,4).join(', ')+' - type a bit more.'); return null; }
 alert('"'+v+'" is not in the skill list - pick an entry from the dropdown.');
 return null;
}
function addSkill(){
 const inp=document.getElementById('skillpick'); const v=inp.value.trim();
 if(!v) return;
 const c = canonSkill(v);
 if(!c) return;
 if(!chosenSkills.includes(c)) chosenSkills.push(c);
 inp.value=''; renderSkills();
}
function addBlocked(){
 const inp=document.getElementById('blockpick'); const v=inp.value.trim();
 if(!v) return;
 const c = canonSkill(v);
 if(!c) return;
 if(!blockedSkills.includes(c)) blockedSkills.push(c);
 inp.value=''; renderBlocked();
}
document.addEventListener('DOMContentLoaded',()=>{
 document.getElementById('skillpick').addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); addSkill(); }});
 document.getElementById('blockpick').addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); addBlocked(); }});
});
function fmt(sec){
 sec=Math.max(0,Math.round(sec));
 const m=Math.floor(sec/60), s=sec%60;
 return (m<10?'0':'')+m+':'+(s<10?'0':'')+s;
}
function paint(){
 const eyebrow=document.getElementById('eyebrow'), timer=document.getElementById('timer'),
       sub=document.getElementById('sub'), btn=document.getElementById('mainbtn');
 if(!running){
  eyebrow.textContent='Ready';
  timer.textContent='--:--';
  sub.textContent = careers>0 ? `stopped - ${careers} career${careers>1?'s':''} done` : 'press start to begin looping careers';
  btn.textContent='Start'; btn.classList.remove('stop');
  return;
 }
 btn.textContent='Stop'; btn.classList.add('stop');
 const rem = srvRemaining - (Date.now()-srvAt)/1000;
 if(srvRemaining>0 && rem>0){
  eyebrow.textContent='Next wake';
  timer.textContent=fmt(rem);
  sub.innerHTML=`<span class="dot"></span>training in game &middot; career #${careers+1} &middot; ${srvState}`;
 }else{
  eyebrow.textContent='Working';
  timer.textContent='&bull;&bull;&bull;';
  timer.innerHTML='&middot;&middot;&middot;';
  sub.innerHTML=`<span class="dot"></span>${srvState} &middot; careers done: ${careers}`;
 }
}
async function refresh(){
 try{
  const r=await fetch('/api/status'); const d=await r.json();
  running=d.running; srvRemaining=d.sleep_remaining||0; srvAt=Date.now();
  srvState=d.state||'...'; careers=d.careers_done||0;
  const lg=document.getElementById('log'); const atBottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-30;
  lg.textContent=d.log.join('\\n'); if(atBottom) lg.scrollTop=lg.scrollHeight;
  const lines=d.log||[];
  document.getElementById('tickertext').textContent = lines.length? lines[lines.length-1] : 'no activity yet';
  const mini=document.getElementById('mini');
  const last8=lines.slice(-8);
  mini.innerHTML = last8.map((l,i)=>`<div${i===last8.length-1?' style="color:#d6dde5"':''}>${l.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`).join('');
  mini.scrollTop = mini.scrollHeight;
 }catch(e){}
 paint();
}
function toggleMini(){
 const m=document.getElementById('mini');
 const open = m.style.display!=='none';
 m.style.display = open?'none':'block';
 document.getElementById('caret').innerHTML = open?'&#9662;':'&#9652;';
 try{ localStorage.setItem('batibot_logopen', open?'0':'1'); }catch(e){}
}
try{ if(localStorage.getItem('batibot_logopen')==='1'){ toggleMini(); } }catch(e){}
setInterval(paint,1000); setInterval(refresh,3000);
async function loadSettings(){
 const r=await fetch('/api/settings'); const s=await r.json();
 for(const k of ['adb_address','adb_path','borrow_name','max_careers']) document.getElementById(k).value=s[k]||'';
 chosenSkills=(s.skills||[]).slice(); renderSkills();
 blockedSkills=(s.skills_blocked||[]).slice(); renderBlocked();
 document.getElementById('auto_reroll').checked=!!s.auto_reroll;
 document.getElementById('recover_tp').checked=!!s.recover_tp;
 document.getElementById('recover_tp_carats_only').checked=!!s.recover_tp_carats_only;
 document.getElementById('it_focus').value=s.it_focus||'';
 document.getElementById('borrow_backup').value=s.borrow_backup||'';
 document.getElementById('spend_all_sp').checked=s.spend_all_sp!==false;
 document.getElementById('smart_skills').checked=s.smart_skills!==false;
 document.getElementById('load_agenda').checked=!!(s.agenda_name||'').trim();
}
async function save(){
 const bn=document.getElementById('borrow_name').value.trim();
 if(bn && ALL_CARDS.length && !ALL_CARDS.some(c=>c.name===bn)){
  if(!confirm('"'+bn+'" is not in the card list (it may be a brand-new banner card). Use it anyway?')) return false;
 }
 const s={
  adb_address:document.getElementById('adb_address').value.trim(),
  adb_path:document.getElementById('adb_path').value.trim()||'adb',
  borrow_name:bn,
  agenda_name:document.getElementById('load_agenda').checked?'TOP':'',
  max_careers:parseInt(document.getElementById('max_careers').value)||0,
  skills:chosenSkills.slice(),
  skills_blocked:blockedSkills.slice(),
  spend_all_sp:document.getElementById('spend_all_sp').checked,
  smart_skills:document.getElementById('smart_skills').checked,
  auto_reroll:document.getElementById('auto_reroll').checked,
  recover_tp:document.getElementById('recover_tp').checked,
  recover_tp_carats_only:document.getElementById('recover_tp_carats_only').checked,
  it_focus:document.getElementById('it_focus').value,
  borrow_backup:document.getElementById('borrow_backup').value.trim()
 };
 await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)});
}
async function clearLog(){
 await fetch('/api/clearlog',{method:'POST'});
 document.getElementById('log').textContent='';
 document.getElementById('mini').innerHTML='';
 document.getElementById('tickertext').textContent='log cleared';
}
let lastHist=0;
async function loadHistory(){
 try{
  const r=await fetch('/api/history'); const h=await r.json();
  if(!h.length){ return; }
  let totalFans=0, best=0;
  h.forEach(e=>{ totalFans+=Number(e.fans)||0; best=Math.max(best, Number(e.rating)||0); });
  const rows = h.slice().reverse().map(e=>{
   const st=e.stats||{};
   const stats=['spd','sta','pow','gut','wit'].map(k=>st[k]==null?'-':st[k]).join(' / ');
   const grade=e.grade?`<span class="grade">${e.grade}</span>`:'';
   const rec=e.races?`${e.wins||0}/${e.races}`:'-';
   const sp=(e.sparks||'').replace(/</g,'&lt;');
   return `<tr>
     <td class="dim">${e.n||''}</td>
     <td>${(e.trainee||'unknown').replace(/</g,'&lt;')}
       ${sp?`<div class="sparks" title="${sp.replace(/"/g,'&quot;')}">${sp}</div>`:''}</td>
     <td class="num">${grade||'<span class="dim">-</span>'}${e.rating?`<div class="dim">${Number(e.rating).toLocaleString()}</div>`:''}</td>
     <td class="num mono">${stats}</td>
     <td class="num">${rec}</td>
     <td class="num">${e.fans?Number(e.fans).toLocaleString():'-'}</td>
     <td class="dim nowrap">${e.ts||''}</td>
   </tr>`;}).join('');
  document.getElementById('history').innerHTML = `
   <div class="histsum">${h.length} career${h.length>1?'s':''}
     &middot; ${totalFans.toLocaleString()} fans total
     &middot; best rating ${best?best.toLocaleString():'-'}
     ${h.length>10?'&middot; newest first, scroll for older':''}</div>
   <div class="histwrap"><table class="hist">
    <thead><tr><th>#</th><th>Trainee / sparks kept</th><th>Grade</th>
      <th>SPD / STA / POW / GUT / WIT</th><th>Wins</th><th>Fans</th><th>Finished</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
 }catch(e){}
}
async function saveClicked(){
 if(await save()===false) return;
 const b=document.getElementById('savebtn'); const old=b.textContent;
 b.textContent='Saved \\u2713'; b.style.color='#1D9E75'; b.style.borderColor='#1D9E75';
 setTimeout(()=>{ b.textContent=old; b.style.color=''; b.style.borderColor=''; },1500);
}
async function mainAction(){
 if(running){ await fetch('/api/stop',{method:'POST'}); }
 else{ if(await save()===false) return; await fetch('/api/start',{method:'POST'}); }
 setTimeout(refresh,600);
}
loadSkills().then(loadSettings); refresh(); loadHistory(); setInterval(loadHistory, 30000);
</script>
</body></html>"""


def make_app(get_bot, start_bot, stop_bot, settings, save_settings, logbuf):
    app = Flask("uma-it-bot")

    @app.get("/")
    def index():
        return PAGE

    @app.get("/api/status")
    def status():
        bot = get_bot()
        rem = 0
        if bot and bot.sleep_until:
            rem = max(0, int(bot.sleep_until - time.time()))
        return jsonify({
            "running": bool(bot and bot.running()),
            "state": bot.state if bot else "idle",
            "careers_done": bot.careers_done if bot else 0,
            "sleep_remaining": rem,
            "log": logbuf[-200:],
        })

    @app.get("/api/skills")
    def skills():
        try:
            with open("skills.json", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            return jsonify([])

    @app.get("/api/cards")
    def cards():
        try:
            with open("cards.json", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception:
            return jsonify([])

    @app.get("/api/history")
    def history():
        try:
            with open("history.json", encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            return jsonify([])
        # letter grade from the rating, using the game's own thresholds
        # (ranks.json, extracted from master.mdb single_mode_rank). This
        # also backfills careers recorded before grades were captured.
        try:
            with open("ranks.json", encoding="utf-8") as f:
                ranks = json.load(f)
        except Exception:
            ranks = []
        for e in hist:
            if not e.get("grade") and e.get("rating") and ranks:
                r = int(e["rating"])
                for lo, hi, name in ranks:
                    if lo <= r <= hi:
                        e["grade"] = name
                        break
        return jsonify(hist)

    @app.post("/api/clearlog")
    def clearlog():
        del logbuf[:]
        return jsonify({"ok": True})

    @app.get("/api/settings")
    def get_settings():
        return jsonify(settings)

    @app.post("/api/settings")
    def set_settings():
        data = request.get_json(force=True) or {}
        settings.update(data)
        save_settings()
        return jsonify({"ok": True})

    @app.post("/api/start")
    def start():
        ok = start_bot()
        return jsonify({"ok": ok})

    @app.post("/api/stop")
    def stop():
        stop_bot()
        return jsonify({"ok": True})

    return app
