// Page script: app logic and hash routing. The effects in visuals.js run in the same closure.
// This is inside a template literal: no backticks, no dollar-brace and no backslashes below.
import { VISUALS } from "./visuals.js";

export function appScript(site) {
  return `(function(){
"use strict";
var SITE="${site}",H={"Content-Type":"application/json","X-FreeCron":"1"};
var $=function(i){return document.getElementById(i)},tok={login:"",job:"",redeem:"",page:""},wid={},mounted={};
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
function sync(){$("login").disabled=!tok.login;$("add").disabled=!tok.job;$("redeem").disabled=!tok.redeem;$("spAdd").disabled=!tok.page}
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
  if(current==="status"&&!mounted.page){mounted.page=1;mount("tsPage","page")}
  if(current==="settings"&&!me.premium&&!mounted.redeem){mounted.redeem=1;mount("tsRedeem","redeem")}
}
function route(fromNav){
  current=viewFromHash();
  VIEWS.forEach(function(n){$("v-"+n).classList.toggle("hidden",n!==current)});
  $$("[data-nav]").forEach(function(a){if(a.dataset.nav===current)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current")});
  document.title="Free Cron // "+TITLES[current];
  setMenu(false);
  ensureMounts();
  if(current==="stats")loadStats();
  if(current==="status")loadPages();
  if(current==="settings")loadAlerts();
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

/* ---------- statistics: inline SVG charts, no library ---------- */
var range="24h",statsSeq=0;
function svg(name,attrs,text){var e=document.createElementNS(NS,name);for(var k in attrs)e.setAttribute(k,attrs[k]);if(text!==undefined)e.textContent=text;return e}
function fmtT(t,size){var d=new Date(t);return size<86400000?(("0"+d.getHours()).slice(-2)+":00"):d.toLocaleDateString(undefined,{month:"short",day:"numeric"})}
function niceMax(v){if(v<=0)return 100;var p=Math.pow(10,Math.floor(Math.log(v)/Math.LN10)),m=v/p;return(m<=1?1:m<=2?2:m<=5?5:10)*p}
var CW=640,CH=220,CL=52,CR=14,CT=14,CB=30,PW=CW-CL-CR,PH=CH-CT-CB;
function frame(label,top,unit){
  var s=svg("svg",{viewBox:"0 0 "+CW+" "+CH,role:"img","aria-label":label});
  for(var i=0;i<=4;i++){var y=CT+PH-PH*i/4;s.appendChild(svg("path",{"class":i?"cg":"ca",d:"M"+CL+" "+y+"H"+(CW-CR)}));s.appendChild(svg("text",{"class":"ct",x:CL-8,y:y+4,"text-anchor":"end"},String(Math.round(top*i/4))+unit))}
  return s;
}
function xLabels(s,r,xAt){
  var n=r.series.length,idx=n>2?[0,Math.floor((n-1)/2),n-1]:[0,n-1];
  idx.forEach(function(i,k){s.appendChild(svg("text",{"class":"ct",x:xAt(i),y:CH-8,"text-anchor":k===0?"start":(k===idx.length-1?"end":"middle")},fmtT(r.series[i].t,r.bucket_ms)))});
}
function drawLine(r){
  var box=$("cLine");box.textContent="";
  var n=r.series.length,max=0,have=0;
  r.series.forEach(function(p){if(p.avg_ms!==null){have++;if(p.avg_ms>max)max=p.avg_ms}});
  var top=niceMax(max),s=frame("Average response time of successful runs, "+r.range,top," ms");
  var xAt=function(i){return CL+(n>1?PW*i/(n-1):PW/2)},yAt=function(v){return CT+PH-PH*v/top};
  if(!have){s.appendChild(svg("text",{"class":"cn",x:CL+PW/2,y:CT+PH/2},"No successful runs in this range"))}
  else{
    var d="",prev=false;
    r.series.forEach(function(p,i){if(p.avg_ms===null){prev=false;return}d+=(prev?"L":"M")+xAt(i).toFixed(1)+" "+yAt(p.avg_ms).toFixed(1)+" ";prev=true});
    s.appendChild(svg("path",{"class":"cl",d:d}));
    r.series.forEach(function(p,i){if(p.avg_ms===null)return;var c=svg("circle",{"class":"cd",cx:xAt(i).toFixed(1),cy:yAt(p.avg_ms).toFixed(1),r:3.2});c.appendChild(svg("title",{},fmtT(p.t,r.bucket_ms)+": "+p.avg_ms+" ms average, "+p.runs+(p.runs===1?" run":" runs")));s.appendChild(c)});
  }
  xLabels(s,r,xAt);box.appendChild(s);
  $("cLineNote").textContent="Average of successful runs per "+(r.bucket_ms<86400000?"hour":"day")+". Timeouts and errors are left out so they don't hide the real speed.";
}
function drawBars(r){
  var box=$("cBars");box.textContent="";
  var n=r.series.length,max=0;r.series.forEach(function(p){if(p.runs>max)max=p.runs});
  var top=max<4?4:niceMax(max),s=frame("Successful and failed runs per "+(r.bucket_ms<86400000?"hour":"day")+", "+r.range,top,"");
  var slot=PW/n,bw=Math.max(3,slot*.64),xAt=function(i){return CL+slot*(i+.5)};
  r.series.forEach(function(p,i){
    if(!p.runs)return;
    var hOk=PH*p.ok/top,hBad=PH*p.failed/top,x=(xAt(i)-bw/2).toFixed(1),base=CT+PH;
    var g=svg("g",{});g.appendChild(svg("title",{},fmtT(p.t,r.bucket_ms)+": "+p.ok+" ok, "+p.failed+" failed"));
    if(p.ok)g.appendChild(svg("rect",{"class":"b-ok",x:x,y:(base-hOk).toFixed(1),width:bw.toFixed(1),height:hOk.toFixed(1)}));
    if(p.failed)g.appendChild(svg("rect",{"class":"b-bad",x:x,y:(base-hOk-hBad).toFixed(1),width:bw.toFixed(1),height:hBad.toFixed(1)}));
    s.appendChild(g)});
  xLabels(s,r,xAt);box.appendChild(s);
}
function drawJobs(r){
  var box=$("sJobs");box.textContent="";
  if(!r.jobs.length){var p=document.createElement("p");p.className="mut";p.textContent="No crons yet.";box.appendChild(p);return}
  r.jobs.forEach(function(j){
    var d=document.createElement("div");d.className="up"+(j.success_rate===null?"":(j.success_rate>=99?" good":(j.success_rate<90?" low":"")));
    var n=document.createElement("span");n.className="un";n.textContent=j.name;
    var pc=document.createElement("span");pc.className="up-pct";pc.textContent=j.success_rate===null?"no runs":(j.success_rate+"%");
    var sub=document.createElement("span");sub.className="up-sub";sub.textContent=j.runs?(j.ok+" of "+j.runs+" runs ok"+(j.avg_ms!==null?(" // avg "+j.avg_ms+" ms"):"")):"Has not run in this range";
    var m=document.createElement("div");m.className="meter";m.setAttribute("aria-hidden","true");meter(m,j.ok,j.runs);
    d.append(n,pc,sub,m);box.appendChild(d)});
}
function drawStats(r){
  var t=r.totals;
  $("sRuns").textContent=String(t.runs);$("sRate").textContent=t.success_rate===null?"-":(t.success_rate+"%");
  $("sAvg").textContent=t.avg_ms===null?"-":(t.avg_ms+" ms");$("sFailed").textContent=String(t.failed);
  var empty=t.runs===0;$("sEmpty").classList.toggle("hidden",!empty);$("sBody").classList.toggle("hidden",empty);
  if(!empty){drawLine(r);drawBars(r);drawJobs(r)}
}
function loadStats(){
  var my=++statsSeq;
  $$(".seg-btn").forEach(function(b){var r=b.dataset.range;b.setAttribute("aria-pressed",r===range?"true":"false");if(r==="30d"){b.disabled=!(me&&me.premium);b.title=b.disabled?"30 days is a Premium feature":""}});
  if(range==="30d"&&!(me&&me.premium))range="24h";
  api("/api/stats?range="+range).then(function(r){if(my===statsSeq){say("");drawStats(r)}}).catch(function(e){if(my===statsSeq)say(e.message,true)});
}
$$(".seg-btn").forEach(function(b){b.addEventListener("click",function(){if(b.disabled)return;range=b.dataset.range;loadStats()})});

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
    var rb=document.createElement("button");rb.className="btn sm";rb.textContent="Run now";
    rb.onclick=function(){rb.disabled=true;api("/api/jobs/"+j.id+"/run",{method:"POST",body:"{}"}).then(function(r){return load().then(function(){say("Ran "+j.name+": "+(r.status?("HTTP "+r.status):"no answer")+" in "+r.ms+" ms.",!r.ok)})}).catch(function(e){fail(e);rb.disabled=false})};
    var eb=document.createElement("button");eb.className="btn sm ghost";eb.textContent="Edit";eb.setAttribute("aria-expanded","false");
    var ed=document.createElement("div");ed.className="hist hidden";
    eb.onclick=function(){var open=ed.classList.contains("hidden");ed.classList.toggle("hidden",!open);eb.setAttribute("aria-expanded",open?"true":"false");if(open&&!ed.firstChild)buildEdit(j,ed)};
    row.append(t,rb,eb,hb,x);d.append(n,u,s,row,ed,hist);box.appendChild(d)});
  if(!booted){booted=true;route(false)}else ensureMounts();
}
/* edit one cron in place: same fields and rules as creating one */
function buildEdit(j,box){
  function field(el){var r=document.createElement("div");r.className="row";r.appendChild(el);return r}
  var nm=document.createElement("input");nm.className="grow";nm.maxLength=60;nm.value=j.name;nm.setAttribute("aria-label","Name");
  var ur=document.createElement("input");ur.className="grow";ur.maxLength=2000;ur.value=j.url;ur.setAttribute("aria-label","URL");
  var me2=document.createElement("select");["GET","POST"].forEach(function(v){var o=document.createElement("option");o.textContent=v;me2.appendChild(o)});me2.value=j.method;me2.setAttribute("aria-label","Method");
  var ev=document.createElement("select");ev.setAttribute("aria-label","How often");
  me.intervals.forEach(function(v){var o=document.createElement("option");o.value=String(v);o.textContent=every(v);o.disabled=v<me.min_minutes;ev.appendChild(o)});
  ev.value=String(j.every_minutes);if(ev.value!==String(j.every_minutes))ev.value=String(me.min_minutes);
  var bd=document.createElement("textarea");bd.className="grow";bd.maxLength=1024;bd.value=j.body||"";bd.setAttribute("aria-label","Body");bd.classList.toggle("hidden",j.method!=="POST");
  me2.onchange=function(){bd.classList.toggle("hidden",me2.value!=="POST")};
  var sv=document.createElement("button");sv.className="btn sm";sv.textContent="Save";
  var cn=document.createElement("button");cn.className="btn sm ghost";cn.textContent="Cancel";
  cn.onclick=function(){box.classList.add("hidden");box.textContent=""};
  sv.onclick=function(){sv.disabled=true;var b={name:nm.value,url:ur.value,method:me2.value,every_minutes:Number(ev.value)};if(b.method==="POST")b.body=bd.value;
    api("/api/jobs/"+j.id,{method:"PATCH",body:JSON.stringify(b)}).then(function(){say("Saved.");return load()}).catch(function(e){fail(e);sv.disabled=false})};
  var act=document.createElement("div");act.className="row";act.append(sv,cn);
  var mr=document.createElement("div");mr.className="row";mr.append(me2,ev);
  box.append(field(nm),field(ur),mr,field(bd),act);
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
/* ---------- failure alerts: the saved webhook is only ever shown masked ---------- */
var alertsOn=true,alertsHook=null;
function drawAlerts(r){
  alertsOn=r.enabled;alertsHook=r.webhook;
  $("alState").textContent=r.webhook?("Webhook saved: "+r.webhook+(r.enabled?". Alerts are on.":". Alerts are paused.")):"No webhook saved yet.";
  $("alTest").disabled=!r.webhook;$("alRemove").disabled=!r.webhook;$("alToggle").disabled=!r.webhook;
  $("alToggle").textContent=r.enabled?"Pause alerts":"Resume alerts";
}
function loadAlerts(){return api("/api/alerts").then(drawAlerts).catch(fail)}
$("alSave").onclick=function(){var v=$("alHook").value.trim();if(!v){say("Paste your Discord webhook address first.",true);return}
  api("/api/alerts",{method:"PUT",body:JSON.stringify({webhook:v})}).then(function(r){$("alHook").value="";say("Webhook saved.");drawAlerts(r)}).catch(fail)};
$("alTest").onclick=function(){$("alTest").disabled=true;api("/api/alerts/test",{method:"POST",body:"{}"}).then(function(){say("Test alert sent. Check your Discord channel.")}).catch(fail).then(function(){$("alTest").disabled=!alertsHook})};
$("alToggle").onclick=function(){api("/api/alerts",{method:"PUT",body:JSON.stringify({enabled:!alertsOn})}).then(drawAlerts).catch(fail)};
$("alRemove").onclick=function(){if(confirm("Remove the saved webhook?"))api("/api/alerts",{method:"PUT",body:JSON.stringify({webhook:null})}).then(function(r){say("Webhook removed.");drawAlerts(r)}).catch(fail)};

/* ---------- status pages: create, rename, switch on/off, choose which crons appear (labels only, never URLs) ---------- */
function mk(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function loadPages(){return api("/api/status-pages").then(function(r){say("");drawPages(r)}).catch(fail)}
function drawPages(r){
  var box=$("pages");box.textContent="";
  $("spNote").textContent="Public pages show only the labels you choose, whether each is up or down, and its uptime. Never your URLs. Without a custom address the link is random and hard to guess. Pages are hidden from search engines. Your plan: "+r.max_pages+(r.max_pages===1?" page":" pages")+", "+r.max_monitors+" crons each.";
  if(!r.pages.length){box.appendChild(mk("p","mut","No status pages yet."));return}
  r.pages.forEach(function(p){
    var d=mk("div","job"),n=mk("div","name");
    n.appendChild(mk("span","dot "+(p.enabled&&!p.blocked?"":"idle")));n.append(p.title);
    n.append(" ",mk("span","badge",p.blocked?"switched off by the service":(p.enabled?"public":"off")));
    var u=mk("div","url mut purl"),a=mk("a","",location.origin+p.path);a.href=p.path;a.target="_blank";a.rel="noopener";u.appendChild(a);
    var row=mk("div","row");
    var rn=mk("button","btn sm","Rename");
    rn.onclick=function(){var t=prompt("New title",p.title);if(t!==null)api("/api/status-pages/"+p.id,{method:"PATCH",body:JSON.stringify({title:t})}).then(loadPages).catch(fail)};
    var tg=mk("button","btn sm",p.enabled?"Switch off":"Switch on");tg.disabled=p.blocked;
    tg.onclick=function(){api("/api/status-pages/"+p.id,{method:"PATCH",body:JSON.stringify({enabled:!p.enabled})}).then(loadPages).catch(fail)};
    var x=mk("button","btn sm ghost","Delete");
    x.onclick=function(){if(confirm("Delete this status page?"))api("/api/status-pages/"+p.id,{method:"DELETE"}).then(loadPages).catch(fail)};
    row.append(rn,tg,x);
    var ms=mk("div","pmons");
    if(!p.monitors.length)ms.appendChild(mk("p","mut fine","No crons on this page yet."));
    var listed={};
    p.monitors.forEach(function(m){
      listed[m.job_id]=1;
      var mr=mk("div","pmon"),l=mk("span","pl",m.label),rm=mk("button","btn sm ghost","Remove");
      rm.onclick=function(){api("/api/status-pages/"+p.id+"/monitors/"+m.job_id,{method:"DELETE"}).then(loadPages).catch(fail)};
      mr.append(l,rm);ms.appendChild(mr)});
    d.append(n,u,row,ms);
    var free=(me&&me.jobs?me.jobs:[]).filter(function(j){return!listed[j.id]});
    if(free.length&&p.monitors.length<r.max_monitors){
      var ar=mk("div","row padd"),sel=mk("select"),lab=mk("input");
      sel.setAttribute("aria-label","Cron to show");lab.setAttribute("aria-label","Public label");lab.maxLength=60;lab.placeholder="Public label";
      free.forEach(function(j){var o=mk("option","",j.name);o.value=String(j.id);sel.appendChild(o)});
      lab.value=free[0].name;
      sel.onchange=function(){var j=free.filter(function(q){return String(q.id)===sel.value})[0];if(j)lab.value=j.name};
      var ab=mk("button","btn sm","Add to page");
      ab.onclick=function(){api("/api/status-pages/"+p.id+"/monitors",{method:"POST",body:JSON.stringify({job_id:Number(sel.value),label:lab.value})}).then(loadPages).catch(fail)};
      ar.append(sel,lab,ab);d.appendChild(ar)}
    else if(!free.length&&p.monitors.length<r.max_monitors)d.appendChild(mk("p","mut fine","All your crons are on this page. Create more crons to add them."));
    box.appendChild(d)});
}
$("spAdd").onclick=function(){
  $("spAdd").disabled=true;
  var b={title:$("spTitle").value,token:tok.page},sl=$("spSlug").value.trim();if(sl)b.slug=sl;
  api("/api/status-pages",{method:"POST",body:JSON.stringify(b)}).then(function(){$("spTitle").value="";$("spSlug").value="";say("Status page created.");return loadPages()}).catch(fail).then(function(){resetTs("page")})};

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
