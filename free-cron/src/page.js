// The single page. Strict CSP: one nonce for the scripts and the style block, no inline style attributes, no innerHTML anywhere.
// Look and feel match the PRIME BOT website (website/assets/site.css): same tokens, fonts, HUD hero, background network and scroll animations.
const esc = (s) => String(s).replace(/[^A-Za-z0-9_.-]/g, "");

export function pageHtml(nonce, siteKey) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Free Cron: 5 free scheduled URL calls</title>
<meta name="description" content="Call any public URL on a schedule. Sign in with GitHub, get 5 free crons. Premium is unlimited.">
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#020407">
<script nonce="${nonce}">document.documentElement.classList.add("js")</script>
<style nonce="${nonce}">
:root{--bg:#020407;--bg2:#06101a;--glow:rgba(150,210,255,.75);--glow-soft:rgba(120,190,255,.28);--line:rgba(225,243,255,.92);--faint:rgba(190,225,255,.38);--hair:rgba(190,225,255,.16);--text:#e4f1fb;--muted:rgba(196,222,242,.74);--ok:#7dffb0;--bad:#ff8f8f;
--mono:"Cascadia Mono","SF Mono",Consolas,"Liberation Mono",Menlo,ui-monospace,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--wrap:1180px;--hdr:64px}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:1.0625rem;line-height:1.6;overflow-x:hidden;min-height:100vh;display:flex;flex-direction:column}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse at 50% 0%,#0a1a2a 0%,rgba(2,4,7,0) 60%),radial-gradient(ellipse at 50% 110%,rgba(120,170,220,.16),transparent 60%)}
body::after{content:"";position:fixed;inset:0;z-index:60;pointer-events:none;opacity:.5;background:repeating-linear-gradient(0deg,rgba(255,255,255,.028) 0 1px,transparent 1px 3px)}
#bgfx{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;opacity:.55}
main,.site-footer,.site-header{position:relative;z-index:1}main{flex:1}
img,svg{max-width:100%}
a{color:#bfe3ff;text-decoration-color:rgba(150,210,255,.5);text-underline-offset:3px}a:hover{color:#fff}
:focus-visible{outline:2px solid #bfe3ff;outline-offset:3px;border-radius:2px}
.wrap{width:min(100% - 2.5rem,var(--wrap));margin-inline:auto}.narrow{width:min(100% - 2.5rem,780px);margin-inline:auto}
h1,h2,h3{font-family:var(--mono);letter-spacing:.06em;line-height:1.2;margin:0 0 .6em;text-shadow:0 0 12px var(--glow-soft);font-weight:600}
h2{font-size:1.15rem;text-transform:uppercase;letter-spacing:.09em}
p{margin:0 0 1em}.muted,.mut{color:var(--muted)}
.hidden,[hidden]{display:none!important}
.eyebrow{font-family:var(--mono);font-size:.78rem;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);margin:0 0 .9rem;display:flex;align-items:center;gap:.7rem}
.eyebrow::before{content:"";width:28px;height:1px;background:var(--line);box-shadow:0 0 6px var(--glow)}
code{font-family:var(--mono);font-size:.88em;background:rgba(150,210,255,.1);border:1px solid var(--hair);padding:.08rem .38rem;border-radius:2px;color:#e8f6ff}

/* header */
.site-header{position:sticky;top:0;z-index:50;height:var(--hdr);background:rgba(2,4,7,.72);backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);border-bottom:1px solid var(--hair)}
.site-header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,var(--line),transparent);opacity:.5}
.hdr-in{height:100%;display:flex;align-items:center;gap:1.2rem;justify-content:space-between}
.brand{font-family:var(--mono);font-weight:700;letter-spacing:.12em;font-size:.92rem;color:#fff;text-decoration:none;text-shadow:0 0 12px var(--glow);white-space:nowrap;display:flex;align-items:center;gap:.6rem}
.brand svg{width:26px;height:26px;flex:none}
.nav{display:flex;align-items:center;gap:.2rem}
.nav a{font-family:var(--mono);font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);text-decoration:none;padding:.55rem .7rem}
.nav a:hover{color:#fff;text-shadow:0 0 10px var(--glow)}
@media (max-width:560px){.nav a.opt{display:none}}

/* buttons */
.btn{position:relative;display:inline-flex;align-items:center;justify-content:center;gap:.6rem;min-height:48px;padding:.8rem 1.5rem;font-family:var(--mono);font-size:.82rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600;text-decoration:none;color:#fff;background:rgba(6,16,26,.7);border:1px solid var(--line);overflow:hidden;cursor:pointer;box-shadow:0 0 14px var(--glow-soft),inset 0 0 14px rgba(150,210,255,.08);transition:box-shadow .25s,transform .25s,color .25s}
.btn::after{content:"";position:absolute;top:0;bottom:0;left:-60%;width:40%;background:linear-gradient(100deg,transparent,rgba(255,255,255,.55),transparent);transform:skewX(-20deg);transition:left .6s}
.btn:hover:not(:disabled){box-shadow:0 0 26px var(--glow),inset 0 0 18px rgba(150,210,255,.18);transform:translateY(-1px);color:#fff}
.btn:hover:not(:disabled)::after{left:130%}
.btn.primary{background:#eaf6ff;color:#02060a;border-color:#fff}
.btn.primary:hover:not(:disabled){color:#000;box-shadow:0 0 34px rgba(190,230,255,.9)}
.btn.ghost{background:transparent}.btn.sm{min-height:38px;padding:.4rem .9rem;font-size:.7rem}
.btn:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:.7rem;flex-wrap:wrap;align-items:center;margin:0 0 .8rem}.grow{flex:1 1 220px}

/* framed cards with corner brackets */
.card{position:relative;padding:1.5rem 1.4rem;margin:0 0 1.3rem;background:rgba(4,10,18,.62);border:1px solid var(--faint);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);transition:box-shadow .3s}
.card::before,.card::after{content:"";position:absolute;width:14px;height:14px;border:2px solid var(--line);transition:width .3s,height .3s}
.card::before{left:-2px;top:-2px;border-right:0;border-bottom:0}.card::after{right:-2px;bottom:-2px;border-left:0;border-top:0}
.card:hover{box-shadow:0 0 28px var(--glow-soft)}.card:hover::before,.card:hover::after{width:26px;height:26px}
.card p:last-child{margin-bottom:0}

/* form controls */
input,select,textarea{font-family:var(--mono);font-size:.92rem;color:#fff;background:rgba(4,10,18,.8);border:1px solid var(--faint);border-radius:0;padding:.7rem 1rem;min-height:46px;min-width:0;color-scheme:dark}
input.grow,textarea{width:100%}textarea{min-height:76px;resize:vertical}
input::placeholder,textarea::placeholder{color:rgba(196,222,242,.55)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--line);box-shadow:0 0 14px var(--glow-soft)}
select option{background:#06101a;color:#fff}

/* hero */
.hero{min-height:min(64svh,560px);display:grid;align-items:center;padding:2rem 0 2.2rem;overflow:hidden;position:relative}
.hero-stage{position:absolute;left:50%;top:46%;width:min(150vw,1100px);transform:translate(-50%,-50%);opacity:.5;pointer-events:none;-webkit-mask-image:radial-gradient(closest-side,#000 55%,transparent 100%);mask-image:radial-gradient(closest-side,#000 55%,transparent 100%)}
.hero-stage svg{display:block;width:100%;height:auto;overflow:visible;filter:drop-shadow(0 0 10px rgba(120,190,255,.28))}
.glow{filter:drop-shadow(0 0 4px var(--glow)) drop-shadow(0 0 10px rgba(120,190,255,.55))}
.spin{transform-origin:380px 200px;animation:spin var(--s,30s) linear infinite}.spin.rev{animation-direction:reverse}
@keyframes spin{to{transform:rotate(360deg)}}
.pulse{animation:pulse 2.2s ease-in-out infinite;transform-origin:380px 200px}
@keyframes pulse{50%{opacity:.35;transform:scale(.9)}}
.s6{--s:60s}.s28{--s:28s}.s17{--s:17s}.s11{--s:11s}.s35{--s:35s}
.hero-in{position:relative;z-index:2;text-align:center;display:flex;flex-direction:column;align-items:center}
.hud-tag{align-self:flex-start;position:relative;padding:1rem 1.4rem .8rem;margin-bottom:clamp(1rem,4vh,2.4rem);font-family:var(--mono);font-size:clamp(.7rem,2vw,.86rem);letter-spacing:.07em;text-shadow:0 0 10px var(--glow);text-align:left}
.hud-tag svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.hud-tag svg *{fill:none;stroke:var(--line);stroke-width:1.2;vector-effect:non-scaling-stroke;filter:drop-shadow(0 0 3px var(--glow))}
.hud-tag b{font-weight:600}
.hero h1{font-size:clamp(2.2rem,8vw,4.8rem);letter-spacing:.1em;margin:0 0 1rem;text-transform:uppercase;text-shadow:0 0 22px var(--glow),0 0 2px #fff}
.hero .sub{font-family:var(--mono);font-size:clamp(.74rem,1.8vw,.95rem);letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:1rem}
.hero .vp{font-size:clamp(1.02rem,2.1vw,1.25rem);max-width:46ch;margin:0 auto;color:var(--text)}
.in-app .hero{min-height:0;padding:1.4rem 0 .4rem}.in-app .hero .vp,.in-app .hud-tag{display:none}.in-app .hero h1{font-size:clamp(1.6rem,5vw,2.6rem);margin-bottom:.4rem}.in-app .hero .sub{margin-bottom:0}
#msg{min-height:1.5em;font-family:var(--mono);font-size:.82rem;letter-spacing:.06em;margin:.3rem 0 1rem;text-align:center}
.ok{color:var(--ok)}.bad{color:var(--bad)}

/* cracking white paint */
.crack{position:relative;display:inline-block}
.crack .shards{position:absolute;inset:-.12em -.2em;pointer-events:none;z-index:3}
.crack .shards i{position:absolute;inset:0;background:linear-gradient(135deg,#fff,#cfe9ff);will-change:transform,opacity;transform-origin:50% 0}
.crack .seam{position:absolute;left:-2%;right:-2%;top:50%;height:2px;background:#000;z-index:4;pointer-events:none;box-shadow:0 0 14px #fff;transform:scaleX(0);transform-origin:50% 50%}
.crack.in .seam{animation:seam .9s ease-out forwards}
@keyframes seam{0%{transform:scaleX(0);opacity:1}55%{transform:scaleX(1);opacity:1}100%{transform:scaleX(1);opacity:0}}
.crack.in .shards i{animation:fall 1.1s cubic-bezier(.5,.1,.3,1) forwards;animation-delay:calc(.45s + var(--d)*.07s)}
@keyframes fall{0%{transform:translate(0,0) rotate(0);opacity:1}25%{opacity:1}100%{transform:translate(var(--tx),var(--ty)) rotate(var(--r));opacity:0}}
/* scroll reveal */
.js .reveal{opacity:0;transform:translateY(22px);transition:opacity .8s ease,transform .8s ease;transition-delay:calc(var(--i,0)*70ms)}
.js .reveal.in{opacity:1;transform:none}

/* crons list */
.job{border-top:1px solid var(--hair);padding:.9rem 0}.job:first-child{border-top:0;padding-top:0}
.name{font-family:var(--mono);font-weight:600;letter-spacing:.04em}
.url{word-break:break-all;font-family:var(--mono);font-size:.84rem;margin:.2rem 0}
.badge{display:inline-block;font-family:var(--mono);font-size:.66rem;letter-spacing:.14em;text-transform:uppercase;padding:.18rem .5rem;border:1px solid var(--faint);color:var(--text);vertical-align:middle;margin-left:.4rem}
.badge.prem{border-color:var(--line);box-shadow:0 0 8px var(--glow-soft)}
.who{font-family:var(--mono);font-size:.84rem;letter-spacing:.06em}
.fine{font-size:.9rem}
.tsbox{min-height:65px}

/* footer */
.site-footer{border-top:1px solid var(--hair);background:rgba(2,4,7,.85);padding:1.6rem 0;margin-top:2rem;font-family:var(--mono);font-size:.74rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.site-footer .wrap{display:flex;flex-wrap:wrap;gap:.6rem 1.6rem;justify-content:space-between}
.site-footer a{color:var(--text);text-decoration:none}.site-footer a:hover{text-shadow:0 0 10px var(--glow)}

/* motion preferences / low power */
.no-motion .spin,.no-motion .pulse{animation:none}.no-motion .btn::after{display:none}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}.spin,.pulse,.crack .seam{animation:none!important}.btn::after{display:none}.js .reveal{opacity:1;transform:none;transition:none}.crack .shards,.crack .seam{display:none}}
@media (pointer:fine) and (hover:hover){.par{will-change:transform}}
</style></head><body>
<canvas id="bgfx" aria-hidden="true"></canvas>
<header class="site-header"><div class="wrap hdr-in">
<a class="brand" href="/"><svg viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="#e1f3ff" stroke-width="1.6"><polygon points="16,2 28,9 28,23 16,30 4,23 4,9"/><polygon points="16,8 23,12 23,20 16,24 9,20 9,12" opacity=".6"/><circle cx="16" cy="16" r="2.6" fill="#e1f3ff"/></svg><span>FREE CRON</span></a>
<nav class="nav" aria-label="Main"><a href="https://prime-bot-site.pages.dev/" rel="noopener">PRIME BOT</a><a class="opt" href="https://prime-bot-site.pages.dev/pricing/" rel="noopener">Pricing</a><a class="opt" href="https://discord.gg/DYfajXrP9B" rel="noopener">Support</a></nav>
</div></header>
<main>
<section class="hero"><div class="hero-stage par" data-par="14" aria-hidden="true"><svg viewBox="0 0 760 400" aria-hidden="true" focusable="false">
<g id="circuits" fill="none" stroke="#e6f4ff" stroke-linecap="square"></g>
<g class="glow" fill="none" stroke="#f2faff">
<polygon points="545,200 462.5,342.9 297.5,342.9 215,200 297.5,57.1 462.5,57.1" stroke-width="2.4"/>
<polygon points="530,200 454.5,330 305.5,330 230,200 305.5,70 454.5,70" stroke-width="1" stroke-dasharray="46 10 8 10" opacity=".6"/>
<g class="spin s6"><text id="bin" font-size="9" fill="#e6f4ff" stroke="none" opacity=".85" textLength="850" lengthAdjust="spacing"><textPath href="#binpath"></textPath></text></g>
<path id="binpath" d="M380 64 a136 136 0 1 1 -0.01 0" stroke="none"/>
<circle class="spin s28" cx="380" cy="200" r="118" stroke-width="9" stroke-dasharray="120 18 60 22 200 24 90 30"/>
<circle class="spin rev s17" cx="380" cy="200" r="99" stroke-width="4" stroke-dasharray="40 8 12 8 220 10"/>
<circle class="spin s11" cx="380" cy="200" r="80" stroke-width="12" stroke-dasharray="210 70 120 100"/>
<circle class="spin rev s35" cx="380" cy="200" r="148" stroke-width="7" stroke-dasharray="1.6 8" opacity=".55"/>
<circle cx="380" cy="200" r="58" stroke-width="1.5" opacity=".8"/>
<circle class="pulse" cx="380" cy="200" r="16" fill="#f2faff" stroke="none"/>
</g></svg></div>
<div class="wrap hero-in">
<div class="hud-tag par" data-par="-8"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><polyline points="100,0 5,0 0,26 0,100"/><polyline points="94,0 100,0"/></svg><b>SYS_NET//FREE_CRON</b><br>//_SCHEDULER.01</div>
<h1><span class="crack">FREE CRON</span></h1>
<p class="sub">Scheduled URL calls // 5 free</p>
<p class="vp">Call any public https URL on a schedule. Sign in with GitHub for <b>5 free crons</b>. Premium is unlimited.</p>
</div></section>

<div class="narrow">
<p id="msg" role="status" class="mut"></p>

<section id="out" class="card hidden reveal">
  <p class="eyebrow">Get started</p>
  <h2>Sign in</h2>
  <p class="muted">We only read your public GitHub id and username. No repository access, nothing is posted.</p>
  <div class="row"><div id="tsLogin" class="tsbox"></div></div>
  <p><button id="login" class="btn primary" disabled>Sign in with GitHub</button></p>
</section>

<section id="app" class="hidden">
  <div class="card reveal"><div class="row"><span id="who" class="grow who"></span><button id="logout" class="btn sm">Sign out</button></div></div>
  <div class="card reveal" id="prem">
    <p class="eyebrow">Upgrade</p>
    <h2>Premium: unlimited crons, every 5 minutes</h2>
    <p id="premState" class="muted"></p>
    <div id="buyBox">
      <p><a id="buy" class="btn primary hidden" href="#" target="_blank" rel="noopener noreferrer">Get Premium on Gumroad</a></p>
      <p class="muted fine">After you buy, Gumroad gives you a license key. Paste it here and Premium switches on by itself.</p>
      <div class="row"><input id="lkey" class="grow" maxlength="80" placeholder="Gumroad license key" aria-label="License key" autocomplete="off"></div>
      <div class="row"><div id="tsRedeem" class="tsbox"></div></div>
      <p><button id="redeem" class="btn" disabled>Activate Premium</button></p>
    </div>
  </div>
  <div class="card reveal">
    <p class="eyebrow">Schedule</p>
    <h2>New cron</h2>
    <div class="row"><input id="name" class="grow" maxlength="60" placeholder="Name (e.g. Keep my app awake)" aria-label="Name"></div>
    <div class="row"><input id="url" class="grow" maxlength="2000" placeholder="https://example.com/ping" aria-label="URL" inputmode="url"></div>
    <div class="row">
      <select id="method" aria-label="Method"><option>GET</option><option>POST</option></select>
      <select id="every" aria-label="How often"></select>
    </div>
    <div class="row"><textarea id="body" class="grow hidden" maxlength="1024" placeholder='Optional JSON body (max 1 KB), e.g. {"hello":"world"}' aria-label="Body"></textarea></div>
    <div class="row"><div id="tsJob" class="tsbox"></div></div>
    <p><button id="add" class="btn primary" disabled>Add cron</button></p>
    <p class="muted fine">Public https addresses only. Redirects aren't followed and responses are never stored. A cron switches itself off after 5 failures in a row.</p>
  </div>
  <div class="card reveal"><p class="eyebrow">Running</p><h2>Your crons</h2><div id="jobs"></div></div>
  <p class="muted"><button id="delacct" class="btn sm ghost">Delete my account and all crons</button></p>
</section>
</div>
</main>
<footer class="site-footer"><div class="wrap"><span>FREE CRON // part of PRIME BOT</span><span><a href="https://prime-bot-site.pages.dev/privacy/" rel="noopener">Privacy</a> &nbsp;·&nbsp; <a href="https://prime-bot-site.pages.dev/terms/" rel="noopener">Terms</a></span></div></footer>

<script nonce="${nonce}" src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
<script nonce="${nonce}">
(function(){
"use strict";
var SITE="${esc(siteKey)}",H={"Content-Type":"application/json","X-FreeCron":"1"};
var $=function(i){return document.getElementById(i)},tok={login:"",job:"",redeem:""},wid={},mounted={};
var $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s))};
var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
var lowPower=reduce||innerWidth<700||(navigator.hardwareConcurrency&&navigator.hardwareConcurrency<=4)||(navigator.connection&&navigator.connection.saveData);
if(lowPower)document.documentElement.classList.add("no-motion");

/* ---------- app logic ---------- */
function say(t,bad){var m=$("msg");m.textContent=t||"";m.className=bad?"bad":"mut"}
function api(p,o){o=o||{};o.headers=H;return fetch(p,o).then(function(r){return r.json().catch(function(){return{}}).then(function(j){if(!r.ok){var e=new Error(j.error||"Something went wrong.");e.status=r.status;throw e}return j})})}
function mount(id,key){var tries=0;(function go(){if(window.turnstile){wid[key]=window.turnstile.render("#"+id,{sitekey:SITE,theme:"dark",callback:function(t){tok[key]=t;sync()},"expired-callback":function(){tok[key]="";sync()}})}else if(tries++<100){setTimeout(go,100)}})()}
function resetTs(key){tok[key]="";if(window.turnstile&&wid[key]!==undefined)window.turnstile.reset(wid[key]);sync()}
function sync(){$("login").disabled=!tok.login;$("add").disabled=!tok.job;$("redeem").disabled=!tok.redeem}
var NAMES={5:"5 minutes",15:"15 minutes",30:"30 minutes",60:"hour",360:"6 hours",720:"12 hours",1440:"24 hours"};
function every(m){return NAMES[m]?("every "+NAMES[m]):("every "+m+" min")}
function when(ms){return ms?new Date(ms).toLocaleString():"never"}
function render(me){
  document.body.classList.add("in-app");
  $("out").classList.add("hidden");$("app").classList.remove("hidden");if(!mounted.job){mounted.job=1;mount("tsJob","job")}
  var ps=$("premState");
  if(me.premium){ps.textContent="Premium is active"+(me.premium_until-Date.now()>432000000?(" until "+when(me.premium_until)):"")+".";$("buyBox").classList.add("hidden")}
  else{ps.textContent="Free plan: "+me.limit+" crons, every "+me.min_minutes+" minutes or slower.";$("buyBox").classList.remove("hidden");
    var a=$("buy");if(me.buy_url){a.href=me.buy_url;a.classList.remove("hidden")}else a.classList.add("hidden");
    if(!mounted.redeem){mounted.redeem=1;mount("tsRedeem","redeem")}}
  $("who").textContent="Signed in as "+me.login+" // "+(me.premium?"Premium (unlimited)":("Free: "+me.used+" of "+me.limit+" crons"));
  var sel=$("every");sel.textContent="";
  me.intervals.forEach(function(m){var o=document.createElement("option");o.value=m;o.textContent=every(m);o.disabled=m<me.min_minutes;sel.appendChild(o);if(m===me.min_minutes&&!sel.value)sel.value=m});
  sel.value=String(me.min_minutes);
  var box=$("jobs");box.textContent="";
  if(!me.jobs.length){var p=document.createElement("p");p.className="mut";p.textContent="No crons yet.";box.appendChild(p)}
  me.jobs.forEach(function(j){
    var d=document.createElement("div");d.className="job";
    var n=document.createElement("div");n.className="name";n.textContent=j.name;
    var b=document.createElement("span");b.className="badge";b.textContent=j.over_limit?"over free limit: paused":(j.enabled?every(j.every_minutes):"off");n.append(" ",b);
    var u=document.createElement("div");u.className="url mut";u.textContent=j.method+" "+j.url;
    var s=document.createElement("div");s.className=(j.last_status>=200&&j.last_status<400)?"ok fine":(j.last_run_at?"bad fine":"mut fine");
    s.textContent=j.last_run_at?("Last run "+when(j.last_run_at)+": "+(j.last_status||"no answer")+(j.last_ms?" in "+j.last_ms+" ms":"")):"Not run yet";
    var row=document.createElement("div");row.className="row";
    var t=document.createElement("button");t.className="btn sm";t.textContent=j.enabled?"Pause":"Resume";
    t.onclick=function(){api("/api/jobs/"+j.id,{method:"PATCH",body:JSON.stringify({enabled:!j.enabled})}).then(load).catch(fail)};
    var x=document.createElement("button");x.className="btn sm ghost";x.textContent="Delete";
    x.onclick=function(){api("/api/jobs/"+j.id,{method:"DELETE"}).then(load).catch(fail)};
    row.append(t,x);d.append(n,u,s,row);box.appendChild(d)});
}
function fail(e){say(e.message,true)}
function load(){return api("/api/me").then(function(me){say("");render(me)}).catch(function(e){
  if(e.status===401){document.body.classList.remove("in-app");$("app").classList.add("hidden");$("out").classList.remove("hidden");if(!mounted.login){mounted.login=1;mount("tsLogin","login")}}else fail(e)})}
$("login").onclick=function(){$("login").disabled=true;api("/api/login",{method:"POST",body:JSON.stringify({token:tok.login})}).then(function(r){location.href=r.url}).catch(function(e){fail(e);resetTs("login")})};
$("redeem").onclick=function(){$("redeem").disabled=true;api("/api/redeem",{method:"POST",body:JSON.stringify({key:$("lkey").value,token:tok.redeem})}).then(function(r){$("lkey").value="";say(r.already?"That key was already used on your account.":"Premium is on. Thank you!");return load()}).catch(fail).then(function(){resetTs("redeem")})};
$("logout").onclick=function(){api("/api/logout",{method:"POST"}).then(function(){location.reload()})};
$("method").onchange=function(){$("body").classList.toggle("hidden",$("method").value!=="POST")};
$("add").onclick=function(){
  $("add").disabled=true;
  var b={name:$("name").value,url:$("url").value,method:$("method").value,every_minutes:Number($("every").value),token:tok.job};
  if(b.method==="POST")b.body=$("body").value;
  api("/api/jobs",{method:"POST",body:JSON.stringify(b)}).then(function(){$("name").value="";$("url").value="";$("body").value="";say("Cron added.");return load()}).catch(fail).then(function(){resetTs("job")})};
$("delacct").onclick=function(){if(confirm("Delete your account and every cron? This can't be undone."))api("/api/me",{method:"DELETE"}).then(function(){location.reload()}).catch(fail)};
var q=new URLSearchParams(location.search).get("error");
if(q)say(q==="state"?"Sign-in expired. Please try again.":"GitHub sign-in failed. Please try again.",true);
history.replaceState(null,"","/");
load();

/* ---------- visuals (same effects as the PRIME BOT website) ---------- */
function rng(seed){var s=seed|0;return function(){s|=0;s=s+0x6D2B79F5|0;var t=Math.imul(s^s>>>15,1|s);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
var NS="http://www.w3.org/2000/svg";

/* cracking white paint on the title */
var GRID={t:[0,28,52,77,100],m:[[0,48],[24,56],[47,44],[73,53],[100,46]],b:[0,30,55,80,100]};
function buildCrack(el){
  if(reduce)return;
  var wrap=document.createElement("span");wrap.className="shards";wrap.setAttribute("aria-hidden","true");
  var polys=[],t=GRID.t,m=GRID.m,b=GRID.b,i;
  for(i=0;i<4;i++){polys.push([[t[i],0],[t[i+1],0],m[i+1],m[i]]);polys.push([m[i],m[i+1],[b[i+1],100],[b[i],100]])}
  var r=rng(7+el.textContent.length);
  polys.forEach(function(poly,k){
    var s=document.createElement("i");
    s.style.clipPath="polygon("+poly.map(function(p){return p[0]+"% "+p[1]+"%"}).join(",")+")";
    var cx=poly.reduce(function(a,p){return a+p[0]},0)/4-50,up=k%2===0;
    s.style.setProperty("--tx",(cx*1.2+(r()-.5)*30).toFixed(0)+"px");
    s.style.setProperty("--ty",(up?-40-r()*60:90+r()*120).toFixed(0)+"px");
    s.style.setProperty("--r",((r()-.5)*50).toFixed(0)+"deg");
    s.style.setProperty("--d",String(Math.abs(Math.round(cx/12))+(up?0:1)));
    wrap.appendChild(s)});
  var seam=document.createElement("span");seam.className="seam";seam.setAttribute("aria-hidden","true");
  el.appendChild(wrap);el.appendChild(seam);
}
$$(".crack").forEach(buildCrack);

/* reveal on scroll */
var targets=$$(".reveal,.crack");
if("IntersectionObserver" in window&&!reduce){
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target)}})},{threshold:.15,rootMargin:"0px 0px -6% 0px"});
  targets.forEach(function(t){io.observe(t)});
}else targets.forEach(function(t){t.classList.add("in")});

/* hero HUD: binary ring + circuit traces */
var bin=document.querySelector("#bin textPath");
if(bin){var r1=rng(0x51ed270b),bits="";for(var i=0;i<150;i++)bits+=(r1()<.5?"0":"1")+(i%7===6?" ":"");bin.textContent=bits}
var cg=$("circuits");
if(cg){
  var r2=rng(0x2f1c);
  ["L","R"].forEach(function(side){
    for(var i=0;i<15;i++){
      var dir=side==="L"?-1:1,y0=95+r2()*210,x0=380+dir*(150+r2()*50),len=60+r2()*(side==="L"?150:130),x=x0,y=y0,d="M"+x+" "+y,jogs=1+Math.floor(r2()*3);
      for(var j=0;j<jogs;j++){var seg=len/jogs;x+=dir*seg*(.4+r2()*.6);d+=" H"+x.toFixed(0);if(j<jogs-1||r2()<.5){y+=(r2()<.5?-1:1)*(6+r2()*16);d+=" V"+y.toFixed(0)}}
      var p=document.createElementNS(NS,"path");p.setAttribute("d",d);p.setAttribute("stroke-width",(.7+r2()*.9).toFixed(1));p.setAttribute("opacity",(.35+r2()*.6).toFixed(2));if(r2()<.3)p.setAttribute("stroke-dasharray","6 3 1 3");cg.appendChild(p);
      var b=document.createElementNS(NS,"rect");b.setAttribute("x",(x-2).toFixed(0));b.setAttribute("y",(y-2).toFixed(0));b.setAttribute("width",r2()<.4?7:3);b.setAttribute("height",3);b.setAttribute("fill","#e6f4ff");b.setAttribute("stroke","none");cg.appendChild(b);
    }
  });
}

/* background node network (30fps cap, pauses when the tab is hidden) */
var cv=$("bgfx");
if(cv&&!lowPower){
  var ctx=cv.getContext("2d"),W,Ht,dpr=Math.min(devicePixelRatio||1,1.5),nodes=[],pulses=[],R=rng(99);
  var resize=function(){W=cv.width=Math.floor(innerWidth*dpr);Ht=cv.height=Math.floor(innerHeight*dpr);var n=Math.min(70,Math.floor(innerWidth*innerHeight/26000));nodes=[];for(var i=0;i<n;i++)nodes.push({x:R()*W,y:R()*Ht,vx:(R()-.5)*.25*dpr,vy:(R()-.5)*.25*dpr})};
  resize();addEventListener("resize",resize);
  var last=0,raf=null,LINK=170*dpr;
  var frame=function(t){
    raf=requestAnimationFrame(frame);
    if(t-last<33)return;last=t;
    ctx.clearRect(0,0,W,Ht);
    var i,j,a,b,dx,dy,d;
    ctx.lineWidth=1;
    for(i=0;i<nodes.length;i++){a=nodes[i];a.x+=a.vx;a.y+=a.vy;if(a.x<0||a.x>W)a.vx*=-1;if(a.y<0||a.y>Ht)a.vy*=-1;
      for(j=i+1;j<nodes.length;j++){b=nodes[j];dx=a.x-b.x;dy=a.y-b.y;d=dx*dx+dy*dy;if(d<LINK*LINK){ctx.strokeStyle="rgba(190,225,255,"+(.22*(1-Math.sqrt(d)/LINK)).toFixed(3)+")";ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
        if(pulses.length<14&&Math.random()<.0009)pulses.push({a:a,b:b,p:0})}}
      ctx.fillStyle="rgba(225,243,255,.55)";ctx.fillRect(a.x-1,a.y-1,2*dpr,2*dpr)}
    for(i=pulses.length-1;i>=0;i--){var u=pulses[i];u.p+=.02;if(u.p>=1){pulses.splice(i,1);continue}
      var px=u.a.x+(u.b.x-u.a.x)*u.p,py=u.a.y+(u.b.y-u.a.y)*u.p;ctx.fillStyle="rgba(255,255,255,.95)";ctx.shadowColor="rgba(150,210,255,1)";ctx.shadowBlur=8*dpr;ctx.fillRect(px-2*dpr,py-2*dpr,4*dpr,4*dpr);ctx.shadowBlur=0}
  };
  raf=requestAnimationFrame(frame);
  document.addEventListener("visibilitychange",function(){if(document.hidden){cancelAnimationFrame(raf);raf=null}else if(!raf)raf=requestAnimationFrame(frame)});
}else if(cv){cv.hidden=true}

/* cursor parallax (desktop pointers only) */
if(!reduce&&matchMedia("(pointer:fine) and (hover:hover)").matches){
  var pars=$$(".par");
  if(pars.length){
    var mx=0,my=0,tx=0,ty=0,pr=null;
    var tick=function(){tx+=(mx-tx)*.08;ty+=(my-ty)*.08;pars.forEach(function(el){var k=parseFloat(el.dataset.par||"10");el.style.transform="translate3d("+(tx*k).toFixed(2)+"px,"+(ty*k).toFixed(2)+"px,0)"});pr=(Math.abs(mx-tx)>.001||Math.abs(my-ty)>.001)?requestAnimationFrame(tick):null};
    addEventListener("pointermove",function(e){mx=(e.clientX/innerWidth-.5)*2;my=(e.clientY/innerHeight-.5)*2;if(!pr)pr=requestAnimationFrame(tick)},{passive:true});
  }
}
})();
</script></body></html>`;
}
