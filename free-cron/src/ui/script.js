// Page script: app logic and hash routing. The effects in visuals.js run in the same closure.
// This is inside a template literal: no backticks, no dollar-brace and no backslashes below.
import { VISUALS } from "./visuals.js";

export function appScript(site) {
  return `(function(){
"use strict";
var SITE="${site}",H={"Content-Type":"application/json","X-FreeCron":"1"};
var $=function(i){return document.getElementById(i)},tok={login:"",job:"",redeem:"",},wid={},mounted={};
var $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s))};
var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
var lowPower=reduce||innerWidth<700||(navigator.hardwareConcurrency&&navigator.hardwareConcurrency<=4)||(navigator.connection&&navigator.connection.saveData);
if(lowPower)document.documentElement.classList.add("no-motion");
var NS="http://www.w3.org/2000/svg";

/* ---------- app logic ---------- */
var VIEWS=["dashboard","crons","status","stats","settings"];
var TITLES={dashboard:"Dashboard",crons:"Cronjobs",status:"Status pages",stats:"Statistics",settings:"Settings"};
var me=null,current="",booted=false;
function say(t,bad){var m=$("msg");m.textContent=t||"";m.className=bad?"bad":"mut"}
function api(p,o){o=o||{};o.headers=H;return fetch(p,o).then(function(r){return r.json().catch(function(){return{}}).then(function(j){if(!r.ok){var e=new Error(j.error||"Something went wrong.");e.status=r.status;throw e}return j})})}
function mount(id,key){var tries=0;(function go(){if(window.turnstile){wid[key]=window.turnstile.render("#"+id,{sitekey:SITE,theme:"dark",callback:function(t){tok[key]=t;sync()},"expired-callback":function(){tok[key]=""; sync()}})}else if(tries++<100){setTimeout(go,100)}})()}
function resetTs(key){tok[key]="";if(window.turnstile&&wid[key]!==undefined)window.turnstile.reset(wid[key]);sync()}
function sync(){$("login").disabled=!tok.login;$("add").disabled=!tok.job;$("redeem").disabled=!tok.redeem}
var NAMES={5:"5 minutes",15:"15 minutes",30:"30 minutes",60:"hour",360:"6 hours",720:"12 hours",1440:"24 hours"};
function every(m){return NAMES[m]?("every "+NAMES[m]):("every "+m+" min")}
function when(ms){return ms?new Date(ms).toLocaleString():"never"}

/* ---------- hash routing: #/dashboard #/crons #/status #/stats #/settings ---------- */
function setMenu(open){
  document.body.classList.toggle("menu-open",open);
  $("scrim").hidden=!open;
  var b=$("menuBtn");b.setAttribute("aria-expanded",open?"true":"false");b.setAttribute("aria-label",open?"Close menu":"Open menu");
}
function viewFromHash(){
  var h=location.hash;if(h.indexOf("#/")===0)h=h.slice(2);else h=h.slice(1);
  return VIEWS.indexOf(h)>=0?h:"dashboard";
}
function ensureMounts(){
  if(!me)return;
  if(current==="crons"&&!mounted.job){mounted.job=1;mount("tsJob","job")}
  if(current==="settings"&&!me.premium&&!mounted.redeem){mounted.redeem=1;mount("tsRedeem","redeem")}
}
function route(fromNav){
  current=viewFromHash();
  VIEWS.forEach(function(n){$("v-"+n).classList.toggle("hidden",n!==current)});
  $$("[data-nav]").forEach(function(a){if(a.dataset.nav===current)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current")});
  document.title="Free Cron // "+TITLES[current];
  setMenu(false);
  ensureMounts();
  if(fromNav){
    var head=document.querySelector("#v-"+current+" .vhead");
    if(head){var h2=head.querySelector("h2");if(h2)h2.focus({preventScroll:true});if(head.getBoundingClientRect().top<64)head.scrollIntoView({block:"start"})}
  }
}
addEventListener("hashchange",function(){if(me)route(true)});
$$("[data-nav]").forEach(function(a){a.addEventListener("click",function(){setMenu(false)})});
$("menuBtn").addEventListener("click",function(){setMenu(!document.body.classList.contains("menu-open"))});
$("scrim").addEventListener("click",function(){setMenu(false)});
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&document.body.classList.contains("menu-open")){setMenu(false);$("menuBtn").focus()}});
addEventListener("resize",function(){if(innerWidth>900)setMenu(false)});

/* ---------- segmented plan meter (20 cells) ---------- */
function meter(el,used,limit){
  el.textContent="";el.classList.toggle("max",limit===null);
  var on=limit===null?20:(limit>0?Math.min(20,Math.round(used/limit*20)):0);
  if(limit!==null&&used>0&&on<1)on=1;
  for(var i=0;i<20;i++){var c=document.createElement("i");if(i<on){c.className="on";c.style.setProperty("--n",String(i))}el.appendChild(c)}
}

function render(m){
  me=m;
  document.body.classList.add("in-app");
  $("out").classList.add("hidden");$("app").classList.remove("hidden");
  var unlimited=m.limit===null;
  var ps=$("premState");
  if(m.premium){ps.textContent="Premium is active"+(m.premium_until-Date.now()>432000000?(" until "+when(m.premium_until)):"")+".";$("buyBox").classList.add("hidden")}
  else{ps.textContent="Free plan: "+m.limit+" crons, every "+m.min_minutes+" minutes or slower.";$("buyBox").classList.remove("hidden");
    var a=$("buy");if(m.buy_url){a.href=m.buy_url;a.classList.remove("hidden")}else a.classList.add("hidden")}
  var plan=m.premium?"Premium (unlimited)":("Free: "+m.used+" of "+m.limit+" crons");
  $("who").textContent="Signed in as "+m.login+" // "+plan;
  $("sideWho").textContent=m.login;$("sidePlan").textContent=m.premium?"Premium // unlimited":("Free // "+m.used+" of "+m.limit);
  $("navCount").textContent=String(m.used);
  meter($("sideMeter"),m.used,m.premium?null:m.limit);
  meter($("dMeter"),m.used,m.premium?null:m.limit);
  var st=m.stats||{enabled:0,disabled:0,ok:0,failed:0};
  $("tEnabled").textContent=String(st.enabled);$("tDisabled").textContent=String(st.disabled);$("tOk").textContent=String(st.ok);
  $("tFailed").textContent=String(st.failed);
  $("dWho").textContent=m.login;
  $("dUsed").textContent=String(m.used);$("dOf").textContent=unlimited?"crons // unlimited":("of "+m.limit+" crons");
  $("dPlan").textContent=m.premium?"Premium is on: unlimited crons, as often as every "+m.min_minutes+" minutes.":("Free plan: as often as every "+m.min_minutes+" minutes. Premium removes the limit.");
  $("dLine").textContent=m.used?("You have "+m.used+(m.used===1?" cron":" crons")+" scheduled."):"No crons yet. Create your first one to get started.";
  var sel=$("every");sel.textContent="";
  m.intervals.forEach(function(v){var o=document.createElement("option");o.value=v;o.textContent=every(v);o.disabled=v<m.min_minutes;sel.appendChild(o);if(v===m.min_minutes&&!sel.value)sel.value=v});
  sel.value=String(m.min_minutes);
  var box=$("jobs");box.textContent="";
  if(!m.jobs.length){var p=document.createElement("p");p.className="mut";p.textContent="No crons yet.";box.appendChild(p)}
  m.jobs.forEach(function(j){
    var d=document.createElement("div");d.className="job";
    var n=document.createElement("div");n.className="name";
    var ok=j.last_status>=200&&j.last_status<400;
    var dot=document.createElement("span");dot.className="dot "+(!j.last_run_at||!j.enabled?"idle":(ok?"":"bad"));n.appendChild(dot);
    n.append(j.name);
    var b=document.createElement("span");b.className="badge";b.textContent=j.over_limit?"over free limit: paused":(j.enabled?every(j.every_minutes):"off");n.append(" ",b);
    var u=document.createElement("div");u.className="url mut";u.textContent=j.method+" "+j.url;
    var s=document.createElement("div");s.className=ok?"ok fine":(j.last_run_at?"bad fine":"mut fine");
    s.textContent=j.last_run_at?("Last run "+when(j.last_run_at)+": "+(j.last_status||"no answer")+(j.last_ms?" in "+j.last_ms+" ms":"")):"Not run yet";
    var row=document.createElement("div");row.className="row";
    var t=document.createElement("button");t.className="btn sm";t.textContent=j.enabled?"Pause":"Resume";
    t.onclick=function(){api("/api/jobs/"+j.id,{method:"PATCH",body:JSON.stringify({enabled:!j.enabled})}).then(load).catch(fail)};
    var x=document.createElement("button");x.className="btn sm ghost";x.textContent="Delete";
    x.onclick=function(){api("/api/jobs/"+j.id,{method:"DELETE"}).then(load).catch(fail)};
    var hb=document.createElement("button");hb.className="btn sm ghost";hb.textContent="History";hb.setAttribute("aria-expanded","false");
    var hist=document.createElement("div");hist.className="hist hidden";
    hb.onclick=function(){var open=hist.classList.contains("hidden");hist.classList.toggle("hidden",!open);hb.setAttribute("aria-expanded",open?"true":"false");if(open)loadRuns(j.id,hist)};
    row.append(t,hb,x);d.append(n,u,s,row,hist);box.appendChild(d)});
  if(!booted){booted=true;route(false)}else ensureMounts();
}
/* run history of one cron: time, status, duration, ok or fail (the server keeps no response bodies) */
function loadRuns(id,box){
  box.textContent="";var p0=document.createElement("p");p0.className="mut fine";p0.textContent="Loading...";box.appendChild(p0);
  api("/api/jobs/"+id+"/runs?limit=50").then(function(r){
    box.textContent="";
    if(!r.runs.length){var e=document.createElement("p");e.className="mut fine";e.textContent="No runs yet. The first one happens at the next schedule tick.";box.appendChild(e);return}
    var wrap=document.createElement("div");wrap.className="hist-scroll";
    r.runs.forEach(function(x){
      var row=document.createElement("div");row.className="hrow "+(x.ok?"ok":"bad");
      var d=document.createElement("span");d.className="hdot";
      var t=document.createElement("span");t.className="ht";t.textContent=when(x.ran_at);
      var s=document.createElement("span");s.className="hs";s.textContent=x.status?String(x.status):"no answer";
      var m=document.createElement("span");m.className="hm";m.textContent=x.ms+" ms";
      var k=document.createElement("span");k.className="hk";k.textContent=x.ok?"OK":"FAIL";
      row.append(d,t,s,m,k);wrap.appendChild(row)});
    var note=document.createElement("p");note.className="mut fine";note.textContent="Last "+r.runs.length+(r.runs.length===1?" run":" runs")+". Free keeps 7 days, Premium 30.";
    box.append(wrap,note)
  }).catch(function(e){box.textContent="";var b=document.createElement("p");b.className="bad fine";b.textContent=e.message;box.appendChild(b)});
}
function fail(e){say(e.message,true)}
function load(){return api("/api/me").then(function(m){say("");render(m)}).catch(function(e){
  if(e.status===401){me=null;setMenu(false);document.body.classList.remove("in-app");$("app").classList.add("hidden");$("out").classList.remove("hidden");if(!mounted.login){mounted.login=1;mount("tsLogin","login")}}else fail(e)})}
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
history.replaceState(null,"",location.pathname+location.hash);
load();

${VISUALS}
})();`;
}
