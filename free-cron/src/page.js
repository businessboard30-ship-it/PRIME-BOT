// The single page. Strict CSP: one nonce for the script and the style block, no inline style attributes, no innerHTML anywhere.
const esc = (s) => String(s).replace(/[^A-Za-z0-9_.-]/g, "");

export function pageHtml(nonce, siteKey) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free Cron: 5 free scheduled URL calls</title>
<meta name="description" content="Call any public URL on a schedule. Sign in with GitHub, get 5 free crons. Premium is unlimited.">
<style nonce="${nonce}">
:root{--bg:#fff;--fg:#14161a;--mut:#5b6472;--card:#f4f6f9;--line:#d9dee6;--acc:#2563eb;--bad:#b42318;--ok:#067647}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e8eaee;--mut:#9aa3b2;--card:#171a21;--line:#2a2f3a;--acc:#6ea0ff;--bad:#ff8a80;--ok:#4cd39b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.1rem;margin:0 0 .6em}
.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grow{flex:1 1 220px}
input,select,textarea{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}
input.grow,textarea{width:100%}textarea{min-height:64px;resize:vertical}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:8px 14px;cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button:disabled{opacity:.5;cursor:not-allowed}
.job{border-top:1px solid var(--line);padding:10px 0}.job:first-child{border-top:0}.name{font-weight:600}.url{word-break:break-all;font-size:.9rem}
.ok{color:var(--ok)}.bad{color:var(--bad)}.hidden{display:none}.badge{font-size:.75rem;border:1px solid var(--line);border-radius:99px;padding:1px 8px}
#msg{min-height:1.4em}
</style></head><body><main>
<h1>Free Cron</h1>
<p class="mut">Call any public https URL on a schedule. Sign in with GitHub for <b>5 free crons</b>. Premium is unlimited.</p>
<p id="msg" role="status" class="mut"></p>

<section id="out" class="card hidden">
  <h2>Sign in</h2>
  <p class="mut">We only read your public GitHub id and username. No repository access, nothing is posted.</p>
  <div class="row"><div id="tsLogin"></div></div>
  <p><button id="login" class="pri" disabled>Sign in with GitHub</button></p>
</section>

<section id="app" class="hidden">
  <div class="card"><div class="row"><span id="who" class="grow"></span><button id="logout">Sign out</button></div></div>
  <div class="card">
    <h2>New cron</h2>
    <div class="row"><input id="name" class="grow" maxlength="60" placeholder="Name (e.g. Keep my app awake)" aria-label="Name"></div>
    <div class="row"><input id="url" class="grow" maxlength="2000" placeholder="https://example.com/ping" aria-label="URL" inputmode="url"></div>
    <div class="row">
      <select id="method" aria-label="Method"><option>GET</option><option>POST</option></select>
      <select id="every" aria-label="How often"></select>
    </div>
    <div class="row"><textarea id="body" class="hidden" maxlength="1024" placeholder='Optional JSON body (max 1 KB), e.g. {"hello":"world"}' aria-label="Body"></textarea></div>
    <div class="row"><div id="tsJob"></div></div>
    <p><button id="add" class="pri" disabled>Add cron</button></p>
    <p class="mut">Public https addresses only. Redirects aren't followed and responses are never stored. A cron switches itself off after 5 failures in a row.</p>
  </div>
  <div class="card"><h2>Your crons</h2><div id="jobs"></div></div>
  <p class="mut"><button id="delacct">Delete my account and all crons</button></p>
</section>

<script nonce="${nonce}" src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
<script nonce="${nonce}">
(function(){
var SITE="${esc(siteKey)}",H={"Content-Type":"application/json","X-FreeCron":"1"};
var $=function(i){return document.getElementById(i)},tok={login:"",job:""},wid={},mounted={};
function say(t,bad){var m=$("msg");m.textContent=t||"";m.className=bad?"bad":"mut"}
function api(p,o){o=o||{};o.headers=H;return fetch(p,o).then(function(r){return r.json().catch(function(){return{}}).then(function(j){if(!r.ok){var e=new Error(j.error||"Something went wrong.");e.status=r.status;throw e}return j})})}
function mount(id,key){var tries=0;(function go(){if(window.turnstile){wid[key]=window.turnstile.render("#"+id,{sitekey:SITE,callback:function(t){tok[key]=t;sync()},"expired-callback":function(){tok[key]="";sync()}})}else if(tries++<100){setTimeout(go,100)}})()}
function resetTs(key){tok[key]="";if(window.turnstile&&wid[key]!==undefined)window.turnstile.reset(wid[key]);sync()}
function sync(){$("login").disabled=!tok.login;$("add").disabled=!tok.job}
var NAMES={5:"5 minutes",15:"15 minutes",30:"30 minutes",60:"hour",360:"6 hours",720:"12 hours",1440:"24 hours"};
function every(m){return NAMES[m]?("every "+NAMES[m]):("every "+m+" min")}
function when(ms){return ms?new Date(ms).toLocaleString():"never"}
function render(me){
  $("out").classList.add("hidden");$("app").classList.remove("hidden");if(!mounted.job){mounted.job=1;mount("tsJob","job")}
  $("who").textContent="Signed in as "+me.login+" · "+(me.premium?"Premium (unlimited)":("Free: "+me.used+" of "+me.limit+" crons"));
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
    var s=document.createElement("div");s.className=(j.last_status>=200&&j.last_status<400)?"ok":(j.last_run_at?"bad":"mut");
    s.textContent=j.last_run_at?("Last run "+when(j.last_run_at)+": "+(j.last_status||"no answer")+(j.last_ms?" in "+j.last_ms+" ms":"")):"Not run yet";
    var row=document.createElement("div");row.className="row";
    var t=document.createElement("button");t.textContent=j.enabled?"Pause":"Resume";
    t.onclick=function(){api("/api/jobs/"+j.id,{method:"PATCH",body:JSON.stringify({enabled:!j.enabled})}).then(load).catch(fail)};
    var x=document.createElement("button");x.textContent="Delete";
    x.onclick=function(){api("/api/jobs/"+j.id,{method:"DELETE"}).then(load).catch(fail)};
    row.append(t,x);d.append(n,u,s,row);box.appendChild(d)});
}
function fail(e){say(e.message,true)}
function load(){return api("/api/me").then(function(me){say("");render(me)}).catch(function(e){
  if(e.status===401){$("app").classList.add("hidden");$("out").classList.remove("hidden");if(!mounted.login){mounted.login=1;mount("tsLogin","login")}}else fail(e)})}
$("login").onclick=function(){$("login").disabled=true;api("/api/login",{method:"POST",body:JSON.stringify({token:tok.login})}).then(function(r){location.href=r.url}).catch(function(e){fail(e);resetTs("login")})};
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
})();
</script></main></body></html>`;
}
