// Public status page (GET /s/:slug), rendered on the server as plain HTML with every user string escaped. No script at all.
// Only the owner's title and labels, the up/down state, uptime percentages and the last-checked time are shown. Never a URL.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const WORD = { up: "Operational", down: "Down", degraded: "Partial outage", unknown: "No data yet", paused: "Paused" };
const OVERALL = { up: "All systems operational", down: "Major outage", degraded: "Some systems are down", unknown: "No data yet" };
const pctText = (v) => (v === null || v === undefined ? "no data" : v + "%");
const ago = (t, now) => {
  if (!t) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago";
};

const CSS = `:root{--bg:#020407;--text:#e1f3ff;--muted:#8fa6b8;--hair:rgba(180,220,255,.18);--glow:rgba(120,200,255,.6);--good:#4be3a0;--bad:#ff6b6b;--warn:#ffc44d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:46rem;margin:0 auto;padding:2rem 1.2rem 4rem}
.top{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.74rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between;gap:1rem;border-bottom:1px solid var(--hair);padding-bottom:1rem;margin-bottom:2rem}
a{color:var(--text)}h1{font-size:1.7rem;margin:0 0 1rem;text-shadow:0 0 18px var(--glow);overflow-wrap:anywhere}
.banner{border:1px solid var(--hair);padding:1rem 1.2rem;margin-bottom:1.5rem;font-weight:600}
.banner.up{border-color:var(--good);color:var(--good)}.banner.down{border-color:var(--bad);color:var(--bad)}.banner.degraded{border-color:var(--warn);color:var(--warn)}.banner.unknown{color:var(--muted)}
.mon{border:1px solid var(--hair);padding:.9rem 1.2rem;margin-bottom:.8rem}
.mon h2{font-size:1.02rem;margin:0;display:flex;justify-content:space-between;gap:1rem;overflow-wrap:anywhere}
.st{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.75rem;letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
.st.up{color:var(--good)}.st.down{color:var(--bad)}.st.paused,.st.unknown{color:var(--muted)}
.up-row{display:flex;flex-wrap:wrap;gap:.3rem 1.4rem;color:var(--muted);font-size:.85rem;margin-top:.4rem}
.note{color:var(--muted);font-size:.82rem;margin-top:1.4rem}
.foot{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--hair);color:var(--muted);font-size:.8rem;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem}`;

export function statusHtml(data, nonce, contact = "") {
  const rep = /^(https:\/\/|mailto:)/.test(contact || "") ? `<a href="${esc(contact)}" rel="noopener noreferrer">Report this page</a>` : "";
  const mons = data.monitors.map((m) => `<div class="mon"><h2><span>${esc(m.label)}</span><span class="st ${m.state}">${WORD[m.state]}</span></h2>
<div class="up-row">${data.windows.map((w) => `<span>${w} uptime: ${pctText(m.uptime[w])}</span>`).join("")}<span>Last checked: ${esc(ago(m.last_checked_at, data.updated_at))}</span></div></div>`).join("\n");
  const note = data.windows.includes("30d") ? "" : `<p class="note">Uptime is shown for 24 hours and 7 days. This page keeps ${data.retention_days} days of history.</p>`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(data.title)} | Status</title>
<meta name="robots" content="noindex, nofollow"><meta http-equiv="refresh" content="60"><meta name="color-scheme" content="dark"><meta name="theme-color" content="#020407">
<style nonce="${nonce}">${CSS}</style></head><body><div class="wrap">
<div class="top"><span>FREE CRON // STATUS</span></div>
<h1>${esc(data.title)}</h1>
<div class="banner ${data.overall}">${OVERALL[data.overall]}</div>
${mons || '<p class="note">No monitors on this page yet.</p>'}
${note}
<div class="foot"><span>Powered by FREE CRON</span><a href="/privacy">Privacy</a><a href="/terms">Terms</a>${rep}</div>
</div></body></html>`;
}

export function notFoundHtml(nonce) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Not found</title><meta name="robots" content="noindex, nofollow">
<style nonce="${nonce}">${CSS}</style></head><body><div class="wrap"><div class="top"><span>FREE CRON // STATUS</span></div><h1>Page not found</h1><p class="note">This status page does not exist or is switched off.</p></div></body></html>`;
}
