// Static page markup: header, hero, sign-in card, and the five app views (dashboard, crons, status, stats, settings).
// Views are shown and hidden by hash routing in script.js. Nothing here is user data, so it is a plain string.
export const MARKUP = `<canvas id="bgfx" aria-hidden="true"></canvas>
<header class="site-header"><div class="wrap hdr-in">
<a class="brand" href="/"><svg viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="#e1f3ff" stroke-width="1.6"><polygon points="16,2 28,9 28,23 16,30 4,23 4,9"/><polygon points="16,8 23,12 23,20 16,24 9,20 9,12" opacity=".6"/><circle cx="16" cy="16" r="2.6" fill="#e1f3ff"/></svg><span>FREE CRON</span></a>
<button class="menu-btn" id="menuBtn" type="button" aria-controls="side" aria-expanded="false" aria-label="Open menu"><span></span><span></span><span></span></button>
</div></header>
<div class="scrim" id="scrim" hidden></div>
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

<div class="wrap">
<p id="msg" role="status" class="mut"></p>

<section id="out" class="card out-wrap hidden reveal">
  <p class="eyebrow">Get started</p>
  <h2>Sign in</h2>
  <p class="muted">We only read your public GitHub id and username. No repository access, nothing is posted.</p>
  <div class="row"><div id="tsLogin" class="tsbox"></div></div>
  <p><button id="login" class="btn primary" disabled>Sign in with GitHub</button></p>
</section>

<div id="app" class="shell hidden">
<aside class="side card" id="side" aria-label="Control panel">
  <p class="side-tag"><span class="dot live"></span>SYS_ONLINE</p>
  <nav class="side-nav" aria-label="Sections">
<a href="#/dashboard" data-nav="dashboard"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg><span>Dashboard</span></a>
<a href="#/crons" data-nav="crons"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span>Cronjobs</span><em class="count" id="navCount">0</em></a>
<a href="#/status" data-nav="status"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg><span>Status pages</span></a>
<a href="#/stats" data-nav="stats"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 20V11M11 20V4M17 20v-6M2 20h20"/></svg><span>Statistics</span></a>
<a href="#/settings" data-nav="settings"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h8M18 6h2M4 12h2M12 12h8M4 18h10M20 18h0"/><circle cx="15" cy="6" r="2.2"/><circle cx="9" cy="12" r="2.2"/><circle cx="17" cy="18" r="2.2"/></svg><span>Settings</span></a>
  </nav>
  <div class="side-foot">
    <p class="side-who" id="sideWho"></p>
    <p class="side-plan" id="sidePlan"></p>
    <div class="meter" id="sideMeter" aria-hidden="true"></div>
  </div>
</aside>

<div class="views" id="views">

<section class="view hidden" id="v-dashboard" data-view="dashboard">
  <header class="vhead"><p class="eyebrow">Control room</p><h2 tabindex="-1">Dashboard</h2><p class="muted">Your scheduler at a glance.</p></header>
  <div class="tiles" aria-label="Cronjob summary">
    <a class="tile reveal" href="#/crons"><span class="tile-n" id="tEnabled">0</span><span class="tile-l">Enabled</span></a>
    <a class="tile reveal" href="#/crons"><span class="tile-n" id="tDisabled">0</span><span class="tile-l">Disabled</span></a>
    <a class="tile reveal ok" href="#/crons"><span class="tile-n" id="tOk">0</span><span class="tile-l">Successful</span></a>
    <a class="tile reveal bad" href="#/crons"><span class="tile-n" id="tFailed">0</span><span class="tile-l">Failed</span></a>
  </div>
  <div class="card reveal">
    <p class="bigline"><small>// Welcome back</small><span id="dWho"></span></p>
    <p class="muted" id="dLine"></p>
    <div class="actions"><a class="btn primary" href="#/crons">Create cronjob</a><a class="btn" href="#/settings">Plan and account</a></div>
  </div>
  <div class="grid2">
    <div class="card reveal">
      <p class="eyebrow">Capacity</p>
      <h2>Cron slots</h2>
      <div class="nums"><b id="dUsed">0</b><span id="dOf"></span></div>
      <div class="meter big" id="dMeter" aria-hidden="true"></div>
      <p class="muted fine" id="dPlan"></p>
    </div>
    <div class="card reveal">
      <p class="eyebrow">Modules</p>
      <h2>What is online</h2>
      <ul class="mods">
        <li><span class="dot live"></span><span>Cronjobs<span class="why">Schedule GET and POST calls to any public https URL.</span></span><em class="chip on">Online</em></li>
        <li><span class="dot live"></span><span>Statistics<span class="why">Run history and response-time charts.</span></span><em class="chip on">Online</em></li>
        <li><span class="dot live"></span><span>Status pages<span class="why">Share the uptime of chosen crons on a public page.</span></span><em class="chip on">Online</em></li>
        <li><span class="dot live"></span><span>Failure alerts<span class="why">Get told on Discord when a cron goes down.</span></span><em class="chip on">Online</em></li>
      </ul>
    </div>
  </div>
</section>

<section class="view hidden" id="v-crons" data-view="crons">
  <header class="vhead"><p class="eyebrow">Schedule</p><h2 tabindex="-1">Cronjobs</h2><p class="muted">Create, pause and delete your scheduled calls.</p></header>
  <div class="card reveal">
    <p class="eyebrow">New</p>
    <h2>Create cronjob</h2>
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
</section>

<section class="view hidden" id="v-status" data-view="status">
  <header class="vhead"><p class="eyebrow">Public</p><h2 tabindex="-1">Status pages</h2><p class="muted">Share the uptime of the crons you choose.</p></header>
  <div class="card reveal">
    <p class="eyebrow">New</p><h2>Create a page</h2>
    <div class="row"><input id="spTitle" class="grow" maxlength="60" placeholder="Page title (e.g. My services)" aria-label="Page title"></div>
    <div class="row"><input id="spSlug" class="grow" maxlength="40" placeholder="Optional custom address, e.g. my-services" aria-label="Custom address" autocomplete="off"></div>
    <div class="row"><div id="tsPage" class="tsbox"></div></div>
    <p><button id="spAdd" class="btn primary" disabled>Create page</button></p>
    <p class="muted fine" id="spNote">Public pages show only the labels you choose, whether each is up or down, and its uptime. Never your URLs. Without a custom address the link is random and hard to guess. Pages are hidden from search engines.</p>
  </div>
  <div class="card reveal"><p class="eyebrow">Public</p><h2>Your pages</h2><div id="pages"></div></div>
</section>

<section class="view hidden" id="v-stats" data-view="stats">
  <header class="vhead"><p class="eyebrow">Telemetry</p><h2 tabindex="-1">Statistics</h2><p class="muted">Success rate and response times for your crons.</p></header>
  <div class="card reveal"><div class="row seg" role="group" aria-label="Time range">
    <button class="btn sm seg-btn" type="button" data-range="24h" aria-pressed="true">24 hours</button>
    <button class="btn sm seg-btn" type="button" data-range="7d" aria-pressed="false">7 days</button>
    <button class="btn sm seg-btn" type="button" data-range="30d" aria-pressed="false">30 days <em class="chip">Premium</em></button>
  </div></div>
  <div class="tiles" aria-label="Totals for the range">
    <div class="tile reveal"><span class="tile-n" id="sRuns">0</span><span class="tile-l">Runs</span></div>
    <div class="tile reveal ok"><span class="tile-n" id="sRate">-</span><span class="tile-l">Success rate</span></div>
    <div class="tile reveal"><span class="tile-n" id="sAvg">-</span><span class="tile-l">Avg response</span></div>
    <div class="tile reveal bad"><span class="tile-n" id="sFailed">0</span><span class="tile-l">Failed</span></div>
  </div>
  <div class="card hidden" id="sEmpty">
    <div class="ph">
      <svg viewBox="0 0 600 230" preserveAspectRatio="none" aria-hidden="true"><path class="grid" d="M0 50H600M0 100H600M0 150H600M0 200H600"/><polyline class="trace" points="0,170 70,140 140,155 210,95 280,120 350,60 420,85 490,40 600,70"/></svg>
      <div class="ph-msg">No runs in this range yet<small>Charts appear after your crons have run. Only status, time and duration are kept, never responses.</small></div>
    </div>
  </div>
  <div id="sBody" class="hidden">
    <div class="card reveal"><p class="eyebrow">Latency</p><h2>Response time</h2><div class="chart" id="cLine"></div><p class="muted fine" id="cLineNote"></p></div>
    <div class="card reveal"><p class="eyebrow">Reliability</p><h2>Successes and failures</h2><div class="chart" id="cBars"></div><p class="muted fine">Green = answered 2xx or 3xx. Red = error, timeout or no answer.</p></div>
    <div class="card reveal"><p class="eyebrow">Per cron</p><h2>Uptime</h2><div id="sJobs"></div></div>
  </div>
</section>

<section class="view hidden" id="v-settings" data-view="settings">
  <header class="vhead"><p class="eyebrow">Account</p><h2 tabindex="-1">Settings</h2><p class="muted">Your plan, your account.</p></header>
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
  <div class="card reveal" id="alertsCard">
    <p class="eyebrow">Alerts</p>
    <h2>Failure alerts on Discord</h2>
    <p class="muted fine">Get a message when a cron fails twice in a row, when it is switched off after 5 failures, and when it is back up. Messages never contain your URLs. In Discord: channel settings, Integrations, Webhooks, New Webhook, Copy Webhook URL.</p>
    <p id="alState" class="muted"></p>
    <div class="row"><input id="alHook" class="grow" maxlength="200" placeholder="https://discord.com/api/webhooks/..." aria-label="Discord webhook address" autocomplete="off"></div>
    <div class="row">
      <button id="alSave" class="btn sm">Save webhook</button>
      <button id="alTest" class="btn sm ghost">Send test alert</button>
      <button id="alToggle" class="btn sm ghost">Pause alerts</button>
      <button id="alRemove" class="btn sm ghost">Remove</button>
    </div>
  </div>
  <div class="card danger reveal">
    <p class="eyebrow">Danger zone</p>
    <h2>Delete account</h2>
    <p class="muted fine">Removes your account and every cron. This can't be undone.</p>
    <p><button id="delacct" class="btn sm ghost">Delete my account and all crons</button></p>
  </div>
</section>

</div>
</div>
</div>
</main>
<footer class="site-footer"><div class="wrap"><span>FREE CRON</span><span><a href="/privacy">Privacy</a> &nbsp;·&nbsp; <a href="/terms">Terms</a></span></div></footer>
`;
