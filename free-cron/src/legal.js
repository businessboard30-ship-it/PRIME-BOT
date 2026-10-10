// Static /privacy and /terms pages. Plain strings, no user data. Same strict-CSP style as the main page (own nonce, no inline styles or handlers).
// The contact line is shown only when the owner sets the public var CONTACT_URL (an https link or a mailto: link); nothing is invented.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PRIVACY = `<h1>Privacy</h1>
<p>FREE CRON calls public URLs on a schedule that you set. This page says what the service stores.</p>
<h2>Sign-in</h2>
<p>You sign in with GitHub. The service reads only your public GitHub id and username. It does not read your email address or your repositories.</p>
<h2>What is stored</h2>
<ul>
<li>Your GitHub id and username.</li>
<li>Your crons: name, URL, method and schedule.</li>
<li>Run history: for each run, only the time, the HTTP status and the duration.</li>
<li>If you use Premium: your Gumroad license key, used to check that it is valid.</li>
</ul>
<h2>What is not stored</h2>
<p>Response bodies and response headers from your URLs are never stored.</p>
<h2>Cookies</h2>
<p>One session cookie keeps you signed in. There are no advertising or tracking cookies.</p>
<h2>Third parties</h2>
<p>GitHub (sign-in), Cloudflare (hosting, database and the Turnstile bot check) and Gumroad (Premium payments and license checks).</p>
<h2>Retention and deletion</h2>
<p>Run history is kept for 7 days on the free plan and 30 days on Premium, then deleted. Deleting a cron deletes its history. Deleting your account in Settings deletes your crons, your history and your account record.</p>`;

const TERMS = `<h1>Terms</h1>
<p>By using FREE CRON you agree to these terms.</p>
<h2>The service</h2>
<p>FREE CRON sends HTTP requests to URLs you choose, on a schedule you choose. It is provided as is, with no guarantee of uptime or exact timing.</p>
<h2>Acceptable use</h2>
<ul>
<li>Only call public https URLs that you own or are allowed to call.</li>
<li>Do not use the service to attack, overload, scan or harass any system, or to break the law.</li>
<li>Do not try to reach private or internal addresses, or to get around the limits.</li>
</ul>
<h2>Limits and enforcement</h2>
<p>The free plan has a limit on the number of crons and on how often they run. Crons or accounts that break these terms can be disabled or removed without notice.</p>
<h2>Premium</h2>
<p>Premium is sold through Gumroad. A valid license key unlocks the Premium limits while it stays valid.</p>
<h2>Your data</h2>
<p>See the Privacy page. You can delete your account at any time in Settings.</p>`;

export function legalPage(kind, nonce, contact = "") {
  const body = kind === "privacy" ? PRIVACY : TERMS;
  const c = /^(https:\/\/|mailto:)/.test(contact || "") ? `<h2>Contact</h2><p>For support or to report abuse: <a href="${esc(contact)}" rel="noopener">${esc(contact.replace(/^mailto:/, ""))}</a></p>` : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${kind === "privacy" ? "Privacy" : "Terms"} | Free Cron</title>
<meta name="color-scheme" content="dark"><meta name="theme-color" content="#020407">
<style nonce="${nonce}">
:root{--bg:#020407;--text:#e1f3ff;--muted:#8fa6b8;--hair:rgba(180,220,255,.18);--glow:rgba(120,200,255,.6)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:46rem;margin:0 auto;padding:2rem 1.2rem 4rem}
.top{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;display:flex;justify-content:space-between;gap:1rem;border-bottom:1px solid var(--hair);padding-bottom:1rem;margin-bottom:2rem}
a{color:var(--text)}.top a{text-decoration:none;color:var(--muted)}.top a:hover{color:#fff;text-shadow:0 0 10px var(--glow)}
h1{font-size:1.8rem;margin:0 0 1rem;text-shadow:0 0 18px var(--glow)}h2{font-size:1.05rem;margin:1.8rem 0 .4rem;color:#fff}
p,li{color:#c9dbe8}ul{padding-left:1.2rem}
</style></head><body><div class="wrap">
<div class="top"><a href="/">FREE CRON</a><span><a href="/privacy">Privacy</a> &nbsp;·&nbsp; <a href="/terms">Terms</a></span></div>
${body}
${c}
</div></body></html>`;
}
