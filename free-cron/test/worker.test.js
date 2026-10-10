import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import worker, { runDue, recheckLicenses, pruneRuns, sendAlerts, pruneOutbox } from "../src/index.js";
import * as L from "../src/logic.js";

// A tiny D1 stand-in over node:sqlite (same SQL dialect, RETURNING included).
function makeDb() {
  const sql = new DatabaseSync(":memory:");
  for (const f of ["0001_init.sql", "0002_licenses.sql", "0003_runs.sql", "0004_status_pages.sql", "0005_alerts.sql"]) sql.exec(readFileSync(new URL("../migrations/" + f, import.meta.url), "utf8"));
  const stmt = (q, args = []) => ({
    bind: (...a) => stmt(q, a),
    run: async () => { const r = sql.prepare(q).run(...args); return { meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } }; },
    first: async () => sql.prepare(q).get(...args) ?? null,
    all: async () => ({ results: sql.prepare(q).all(...args) }),
  });
  return { prepare: (q) => stmt(q), batch: async (list) => { for (const s of list) await s.run(); return []; }, sql };
}
const SECRET = "test-session-secret-123456";
const env = (extra = {}) => ({ DB: makeDb(), SESSION_SECRET: SECRET, TURNSTILE_SECRET: "ts", TURNSTILE_SITE_KEY: "site", ADMIN_KEY: "adminkey", GITHUB_CLIENT_ID: "cid", GITHUB_CLIENT_SECRET: "csec", ...extra });
let nextOk = true;
const gumCalls = [];
let gumReply = () => new Response(JSON.stringify({ success: false }), { status: 404 });
const gum = (purchase = {}) => () => new Response(JSON.stringify({ success: true, purchase: { product_id: "prod1", ...purchase } }));
globalThis.fetch = async (u, init) => {
  const url = String(u);
  if (url.includes("gumroad.com/v2/licenses/verify")) { gumCalls.push(String(init.body)); return gumReply(String(init.body)); }
  if (url.includes("siteverify")) return new Response(JSON.stringify({ success: nextOk }));
  return new Response("x", { status: 200 });
};
async function addAccount(e, id = 1, login = "alice", premium = 0) {
  e.DB.sql.prepare("INSERT INTO accounts (id, github_id, login, premium_until, created_at) VALUES (?,?,?,?,0)").run(id, id + 1000, login, premium);
  return L.signToken({ id, exp: Date.now() + 60000 }, SECRET);
}
const call = (e, path, { method = "GET", body, token, headers = {} } = {}) => worker.fetch(new Request("https://fc.example.dev" + path, {
  method, body: body ? JSON.stringify(body) : undefined,
  headers: { ...(method !== "GET" ? { "X-FreeCron": "1", "Content-Type": "application/json" } : {}), ...(token ? { Cookie: `fc_session=${token}` } : {}), ...headers },
}), e);
const job = (o = {}) => ({ name: "Ping", url: "https://example.com/p", method: "GET", every_minutes: 15, token: "cf-token", ...o });

test("page is served with a strict CSP and no inline handlers", async () => {
  const r = await call(env(), "/");
  const csp = r.headers.get("Content-Security-Policy"), html = await r.text();
  assert.ok(csp.includes("default-src 'none'") && csp.includes("frame-ancestors 'none'") && !csp.includes("unsafe-inline"));
  assert.ok(html.includes("challenges.cloudflare.com") && !html.includes("innerHTML") && !/ on\w+=/.test(html) && !/style=/.test(html));
});

test("page has the five app views, each linked from the sidebar, and a mobile menu", async () => {
  const html = await (await call(env(), "/")).text();
  for (const n of ["dashboard", "crons", "status", "stats", "settings"]) {
    assert.ok(html.includes(`href="#/${n}"`) && html.includes(`data-nav="${n}"`), `nav link for ${n}`);
    assert.ok(html.includes(`id="v-${n}"`) && html.includes(`data-view="${n}"`), `view for ${n}`);
  }
  assert.ok(html.includes('id="menuBtn"') && html.includes('aria-controls="side"') && html.includes('id="side"'));
  assert.equal((html.match(/data-view="/g) || []).length, 5);
});

test("every script and style tag in the page carries the request nonce", async () => {
  const r = await call(env(), "/"), csp = r.headers.get("Content-Security-Policy"), html = await r.text();
  const nonce = /'nonce-([^']+)'/.exec(csp)[1];
  const tags = html.match(/<(script|style)\b[^>]*>/g) || [];
  assert.ok(tags.length >= 4);
  for (const t of tags) assert.ok(t.includes(`nonce="${nonce}"`), `missing nonce: ${t}`);
});

test("the script only routes to views that exist", async () => {
  const html = await (await call(env(), "/")).text();
  const list = /var VIEWS=\[([^\]]*)\]/.exec(html)[1].replace(/"/g, "").split(",");
  assert.deepEqual(list, ["dashboard", "crons", "status", "stats", "settings"]);
  for (const n of list) assert.ok(html.includes(`id="v-${n}"`));
});

test("/api/me returns the dashboard tile counts", async () => {
  const e = env(), t = await addAccount(e);
  assert.deepEqual((await (await call(e, "/api/me", { token: t })).json()).stats, { enabled: 0, disabled: 0, ok: 0, failed: 0 });
  nextOk = true;
  for (let i = 0; i < 4; i++) assert.equal((await call(e, "/api/jobs", { method: "POST", body: job({ name: "J" + i }), token: t })).status, 200);
  const ids = e.DB.sql.prepare("SELECT id FROM jobs ORDER BY id").all().map((r) => r.id);
  const set = e.DB.sql.prepare("UPDATE jobs SET last_run_at = ?1, last_status = ?2, enabled = ?3 WHERE id = ?4");
  set.run(Date.now(), 200, 1, ids[0]); set.run(Date.now(), 503, 1, ids[1]); set.run(Date.now(), 200, 0, ids[2]);
  const me = await (await call(e, "/api/me", { token: t })).json();
  assert.deepEqual(me.stats, { enabled: 3, disabled: 1, ok: 2, failed: 1 });
  assert.equal(me.jobs.length, 4);
  const other = await addAccount(e, 2, "bob");
  assert.deepEqual((await (await call(e, "/api/me", { token: other })).json()).stats, { enabled: 0, disabled: 0, ok: 0, failed: 0 });
});

test("dashboard has the four tiles and a create button", async () => {
  const html = await (await call(env(), "/")).text();
  for (const id of ["tEnabled", "tDisabled", "tOk", "tFailed"]) assert.ok(html.includes(`id="${id}"`));
  assert.ok(html.includes('class="btn primary" href="#/crons">Create cronjob'));
});

test("API needs a session; state changes need our header", async () => {
  const e = env();
  assert.equal((await call(e, "/api/me")).status, 401);
  assert.equal((await call(e, "/api/jobs", { method: "POST", body: job() })).status, 401);
  const t = await addAccount(e);
  const noHeader = await worker.fetch(new Request("https://fc.example.dev/api/jobs", { method: "POST", body: "{}", headers: { Cookie: `fc_session=${t}` } }), e);
  assert.equal(noHeader.status, 403);
  const evil = await call(e, "/api/jobs", { method: "POST", body: job(), token: t, headers: { Origin: "https://evil.dev" } });
  assert.equal(evil.status, 403);
  assert.equal((await call(e, "/api/me", { token: "forged.token" })).status, 401);
});

test("captcha is required to sign in and to create; fails closed", async () => {
  const e = env(), t = await addAccount(e);
  nextOk = false;
  assert.equal((await call(e, "/api/login", { method: "POST", body: { token: "x" } })).status, 400);
  assert.equal((await call(e, "/api/jobs", { method: "POST", body: job(), token: t })).status, 400);
  nextOk = true;
  assert.equal((await call(env({ TURNSTILE_SECRET: "" }), "/api/login", { method: "POST", body: { token: "x" } })).status, 400);
  const r = await call(e, "/api/login", { method: "POST", body: { token: "x" } });
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.ok(j.url.startsWith("https://github.com/login/oauth/authorize?") && !j.url.includes("scope="));
  assert.ok(/fc_state=.*HttpOnly.*Secure.*SameSite=Lax/.test(r.headers.get("Set-Cookie")));
});

test("5 free crons, the 6th is refused; premium is unlimited", async () => {
  const e = env(), t = await addAccount(e);
  for (let i = 0; i < 5; i++) assert.equal((await call(e, "/api/jobs", { method: "POST", body: job({ name: "n" + i }), token: t })).status, 200);
  const sixth = await call(e, "/api/jobs", { method: "POST", body: job(), token: t });
  assert.equal(sixth.status, 422);
  assert.match((await sixth.json()).error, /5 free crons/);
  const pe = env(), pt = await addAccount(pe, 2, "bob", Date.now() + 86400000);
  for (let i = 0; i < 12; i++) assert.equal((await call(pe, "/api/jobs", { method: "POST", body: job({ name: "p" + i, every_minutes: 5 }), token: pt })).status, 200, "premium " + i);
  const me = await (await call(pe, "/api/me", { token: pt })).json();
  assert.equal(me.premium, true); assert.equal(me.limit, null); assert.equal(me.used, 12);
});

test("free accounts can't go under 15 minutes; bad urls are rejected before any write", async () => {
  const e = env(), t = await addAccount(e);
  assert.equal((await call(e, "/api/jobs", { method: "POST", body: job({ every_minutes: 5 }), token: t })).status, 422);
  for (const url of ["http://example.com", "https://127.0.0.1/", "https://169.254.169.254/", "https://localhost/", "https://fc.example.dev/api/me"])
    assert.equal((await call(e, "/api/jobs", { method: "POST", body: job({ url }), token: t })).status, 422, url);
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM jobs").get().c, 0);
});

test("creation budget: 20 per day even if crons are deleted", async () => {
  const e = env(), t = await addAccount(e, 3, "carol", Date.now() + 86400000);
  let last = 200;
  for (let i = 0; i < 22; i++) {
    const r = await call(e, "/api/jobs", { method: "POST", body: job({ name: "x" + i }), token: t });
    last = r.status;
    if (r.status === 200) await call(e, "/api/jobs/" + (await r.json()).id, { method: "DELETE", token: t });
    if (r.status === 429) break;
  }
  assert.equal(last, 429);
});

test("accounts are isolated", async () => {
  const e = env(), a = await addAccount(e, 1, "alice"), b = await addAccount(e, 2, "bob");
  const id = (await (await call(e, "/api/jobs", { method: "POST", body: job(), token: a })).json()).id;
  assert.equal((await call(e, "/api/jobs/" + id, { method: "DELETE", token: b })).status, 404);
  assert.equal((await call(e, "/api/jobs/" + id, { method: "PATCH", body: { enabled: false }, token: b })).status, 404);
  assert.equal((await (await call(e, "/api/me", { token: b })).json()).used, 0);
  assert.equal((await call(e, "/api/jobs/" + id, { method: "PATCH", body: { enabled: false }, token: a })).status, 200);
});

test("delete my account removes every cron", async () => {
  const e = env(), t = await addAccount(e);
  await call(e, "/api/jobs", { method: "POST", body: job(), token: t });
  assert.equal((await call(e, "/api/me", { method: "DELETE", token: t })).status, 200);
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM jobs").get().c, 0);
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM accounts").get().c, 0);
});

test("admin premium endpoint: hidden without a key, 401 with the wrong one, works with the right one", async () => {
  const e = env(); await addAccount(e, 1, "Alice");
  assert.equal((await call(env({ ADMIN_KEY: "" }), "/api/admin/premium", { method: "POST", body: { login: "alice", days: 30 } })).status, 404);
  assert.equal((await call(e, "/api/admin/premium", { method: "POST", body: { login: "alice", days: 30 }, headers: { Authorization: "Bearer nope" } })).status, 401);
  assert.equal((await call(e, "/api/admin/premium", { method: "POST", body: { login: "alice", days: 99999 }, headers: { Authorization: "Bearer adminkey" } })).status, 422);
  const ok = await call(e, "/api/admin/premium", { method: "POST", body: { login: "alice", days: 30 }, headers: { Authorization: "Bearer adminkey" } });
  assert.equal((await ok.json()).updated, 1);
  assert.ok(e.DB.sql.prepare("SELECT premium_until p FROM accounts").get().p > Date.now());
});

// ---- the scheduler ----
const NOW = 1_800_000_000_000;
function seed(e, accountId, n, { every = 15, due = true } = {}) {
  for (let i = 0; i < n; i++) e.DB.sql.prepare("INSERT INTO jobs (account_id,name,url,method,every_minutes,next_run_at,created_at) VALUES (?,?,?,?,?,?,0)")
    .run(accountId, "j" + i, "https://example.com/" + i, "GET", every, due ? NOW - 1000 : NOW + 999999);
}
const ranUrls = [];
const okFetch = (status = 200) => async (u, init) => { ranUrls.push([String(u), init]); return new Response(status === 204 ? null : "secret-body", { status }); };

test("scheduler: runs due jobs, advances next_run_at, stores status but never the body", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 2); seed(e, 1, 1, { due: false });
  ranUrls.length = 0;
  const out = await runDue(e, NOW, okFetch(204));
  assert.deepEqual(out, { ran: 2, ok: 2, failed: 0 });
  const rows = e.DB.sql.prepare("SELECT * FROM jobs ORDER BY id").all();
  assert.equal(rows[0].last_status, 204); assert.equal(rows[0].next_run_at, NOW + 15 * 60000); assert.equal(rows[0].fail_count, 0);
  assert.equal(rows[2].last_run_at, null);
  assert.ok(JSON.stringify(rows).indexOf("secret-body") < 0);
  await runDue(e, NOW + 99 * 60000, okFetch(200));          // a 200 with a body: still nothing stored
  assert.ok(JSON.stringify(e.DB.sql.prepare("SELECT * FROM jobs").all()).indexOf("secret-body") < 0);
  const init = ranUrls[0][1];
  assert.equal(init.redirect, "manual"); assert.ok(init.signal); assert.ok(init.headers["User-Agent"].startsWith("free-cron/"));
});

test("scheduler: a second overlapping tick can't re-run claimed jobs", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 3);
  const [a, b] = await Promise.all([runDue(e, NOW, okFetch()), runDue(e, NOW, okFetch())]);
  assert.equal((a.ran || 0) + (b.ran || 0), 3);
});

test("scheduler: failures count, 5 in a row switch the job off, success resets", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 1);
  for (let i = 1; i <= 5; i++) {
    e.DB.sql.prepare("UPDATE jobs SET next_run_at = ? WHERE id = 1").run(NOW - 1);
    await runDue(e, NOW, okFetch(500));
    const r = e.DB.sql.prepare("SELECT fail_count f, enabled en FROM jobs").get();
    assert.equal(r.f, i); assert.equal(r.en, i < 5 ? 1 : 0);
  }
  e.DB.sql.prepare("UPDATE jobs SET enabled = 1, fail_count = 3, next_run_at = ? WHERE id = 1").run(NOW - 1);
  await runDue(e, NOW, okFetch(200));
  assert.equal(e.DB.sql.prepare("SELECT fail_count f FROM jobs").get().f, 0);
});

test("scheduler: network error / timeout counts as a failure with status 0; redirects are not followed", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 2);
  let n = 0;
  await runDue(e, NOW, async () => { if (n++ === 0) throw new Error("boom"); return new Response(null, { status: 302, headers: { Location: "http://127.0.0.1/" } }); });
  const rows = e.DB.sql.prepare("SELECT last_status s FROM jobs ORDER BY id").all().map((r) => r.s).sort();
  assert.deepEqual(rows, [0, 302]);
});

test("scheduler: free accounts only run their first 5; premium runs all; lapsed premium is clamped to 15 min", async () => {
  const e = env(); await addAccount(e, 1, "free"); await addAccount(e, 2, "prem", NOW + 86400000);
  seed(e, 1, 7); seed(e, 2, 8, { every: 5 });
  const out = await runDue(e, NOW, okFetch());
  assert.equal(out.ran, 5 + 8);
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM jobs WHERE account_id = 1 AND last_run_at IS NOT NULL").get().c, 5);
  e.DB.sql.prepare("UPDATE accounts SET premium_until = ? WHERE id = 2").run(NOW - 1);
  e.DB.sql.prepare("UPDATE jobs SET next_run_at = ? WHERE account_id = 2").run(NOW - 1);
  await runDue(e, NOW + 10, okFetch());
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM jobs WHERE account_id = 2 AND last_run_at = ?").get(NOW + 10).c, 5);
  assert.equal(e.DB.sql.prepare("SELECT next_run_at n FROM jobs WHERE account_id = 2 AND last_run_at = ?").get(NOW + 10).n, NOW + 10 + 15 * 60000);
});

test("scheduler: batch cap, kill switch, and corrupt/unsafe rows are never fetched", async () => {
  const e = env(); await addAccount(e, 1, "p", NOW + 86400000); seed(e, 1, 60, { every: 5 });
  assert.equal((await runDue(e, NOW, okFetch())).ran, L.BATCH);
  assert.deepEqual(await runDue(env({ DISABLED: "1" }), NOW, okFetch()), { skipped: "disabled" });
  const e2 = env(); await addAccount(e2);
  e2.DB.sql.prepare("INSERT INTO jobs (account_id,name,url,method,every_minutes,next_run_at,created_at) VALUES (1,'bad','https://127.0.0.1/x','GET',15,?,0)").run(NOW - 1);
  ranUrls.length = 0;
  const out = await runDue(e2, NOW, okFetch());
  assert.equal(out.failed, 1); assert.equal(ranUrls.length, 0);
});

const G = { GUMROAD_PRODUCT_ID: "prod1", GUMROAD_BUY_URL: "https://x.gumroad.com/l/prem" };
const KEY = "ABCD1234-EFGH5678-IJKL9012-MNOP3456";
const redeem = (e, t, key = KEY) => call(e, "/api/redeem", { method: "POST", body: { key, token: "cf" }, token: t });

test("redeem: a valid Gumroad membership key turns Premium on, once, for one account", async () => {
  const e = env(G), t = await addAccount(e), t2 = await addAccount(e, 2, "bob");
  gumReply = gum();
  const r = await redeem(e, t);
  assert.equal(r.status, 200);
  assert.ok(gumCalls.at(-1).includes("increment_uses_count=false") && gumCalls.at(-1).includes("product_id=prod1"));
  const me = await (await call(e, "/api/me", { token: t })).json();
  assert.equal(me.premium, true); assert.equal(me.buy_url, "https://x.gumroad.com/l/prem");
  assert.equal((await redeem(e, t2)).status, 409);                       // already bound to alice
  assert.equal((await redeem(e, t)).status, 200);                        // alice can paste it again harmlessly
  assert.equal((await call(e, "/api/me", { token: t2 })).status, 200);
  assert.equal((await (await call(e, "/api/me", { token: t2 })).json()).premium, false);
});

test("redeem: bad, refunded, cancelled or wrong-product keys are refused; the captcha and setup are required", async () => {
  const e = env(G), t = await addAccount(e);
  gumReply = () => new Response(JSON.stringify({ success: false }), { status: 404 });
  assert.equal((await redeem(e, t)).status, 422);
  for (const p of [{ refunded: true }, { chargebacked: true }, { disputed: true }, { subscription_cancelled_at: "2026-10-01" }, { subscription_failed_at: "2026-10-01" }, { product_id: "other" }]) {
    gumReply = gum(p);
    assert.equal((await redeem(e, t)).status, 422, JSON.stringify(p));
  }
  assert.equal((await redeem(e, t, "short")).status, 422);
  assert.equal((await redeem(e, t, "has spaces and <script>")).status, 422);
  assert.equal((await call(e, "/api/me", { token: t }).then((r) => r.json())).premium, false);
  gumReply = gum(); nextOk = false;
  assert.equal((await redeem(e, t)).status, 400); nextOk = true;
  { const bare = env(), bt = await addAccount(bare); assert.equal((await redeem(bare, bt)).status, 503); }                    // GUMROAD_PRODUCT_ID not set
  assert.equal((await call(e, "/api/redeem", { method: "POST", body: { key: KEY, token: "cf" } })).status, 401);   // no session
  gumReply = () => new Response("oops", { status: 503 });
  assert.equal((await redeem(e, t)).status, 502);                        // Gumroad down: nothing granted
  assert.equal((await call(e, "/api/me", { token: t }).then((r) => r.json())).premium, false);
});

test("one-time purchase: fixed days, granted once", async () => {
  const e = env({ ...G, GUMROAD_RECURRING: "0", PREMIUM_DAYS: "30" }), t = await addAccount(e);
  gumReply = gum();
  assert.equal((await redeem(e, t)).status, 200);
  const first = e.DB.sql.prepare("SELECT premium_until p FROM accounts WHERE id=1").get().p;
  assert.ok(Math.abs(first - (Date.now() + 30 * 86400000)) < 60000);
  assert.deepEqual(await (await redeem(e, t)).json(), { already: true });
  assert.equal(e.DB.sql.prepare("SELECT premium_until p FROM accounts WHERE id=1").get().p, first);
  assert.equal((await recheckLicenses(e, Date.now() + 3 * 86400000)).checked, 0);   // one-time keys are never re-checked
});

test("daily re-check renews a paying member and lets a cancelled one lapse", async () => {
  const e = env(G), t = await addAccount(e), t2 = await addAccount(e, 2, "bob");
  gumReply = gum(); await redeem(e, t);
  gumReply = gum(); await redeem(e, t2, "ZZZZ1111-YYYY2222-XXXX3333-WWWW4444");
  const now = Date.now();
  assert.equal((await recheckLicenses(e, now + 3600000)).checked, 0);               // not due yet
  const day = now + 25 * 3600000;
  gumReply = (b) => b.includes("ABCD1234") ? gum()() : gum({ subscription_cancelled_at: "2026-10-10" })();
  const out = await recheckLicenses(e, day);
  assert.deepEqual(out, { checked: 2, renewed: 1, ended: 1, retry: 0 });
  const alice = e.DB.sql.prepare("SELECT premium_until p FROM accounts WHERE id=1").get().p;
  const bob = e.DB.sql.prepare("SELECT premium_until p FROM accounts WHERE id=2").get().p;
  assert.ok(alice > day + 2 * 86400000);
  assert.ok(bob < now + 3 * 86400000 + 60000 && alice > bob);                         // bob is no longer extended, so he lapses on his own within the grace window
  assert.equal(e.DB.sql.prepare("SELECT active a FROM licenses WHERE account_id=2").get().a, 0);
  gumReply = () => new Response("down", { status: 500 });                           // Gumroad outage must not end anyone
  const later = day + 25 * 3600000;
  const o2 = await recheckLicenses(e, later);
  assert.equal(o2.retry, 1); assert.equal(o2.ended, 0);
  assert.equal(e.DB.sql.prepare("SELECT active a FROM licenses WHERE account_id=1").get().a, 1);
});

test("deleting the account removes its license rows", async () => {
  const e = env(G), t = await addAccount(e);
  gumReply = gum(); await redeem(e, t);
  assert.equal((await call(e, "/api/me", { method: "DELETE", token: t })).status, 200);
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) c FROM licenses").get().c, 0);
});

// ---------- run history ----------
const DAY = 86400000;
const addRun = (e, jobId, accountId, ranAt, status = 200) => e.DB.sql.prepare("INSERT INTO runs (job_id,account_id,ran_at,status,ms,ok) VALUES (?,?,?,?,?,?)").run(jobId, accountId, ranAt, status, 50, status >= 200 && status < 400 ? 1 : 0);
const runsOf = (e) => e.DB.sql.prepare("SELECT * FROM runs ORDER BY id").all();

test("history: every run writes one row with status, time and duration, and never a body", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 3);
  e.DB.sql.prepare("UPDATE jobs SET url = 'https://example.com/hang' WHERE id = 3").run();
  const f = async (u) => { if (String(u).endsWith("/hang")) throw new Error("timeout"); return new Response("secret-body", { status: String(u).endsWith("/1") ? 503 : 200 }); };
  const out = await runDue(e, NOW, f);
  assert.deepEqual(out, { ran: 3, ok: 1, failed: 2 });
  const rows = runsOf(e);
  assert.equal(rows.length, 3);
  const byJob = Object.fromEntries(rows.map((r) => [r.job_id, r]));
  assert.deepEqual([byJob[1].status, byJob[1].ok], [200, 1]);
  assert.deepEqual([byJob[2].status, byJob[2].ok], [503, 0]);
  assert.deepEqual([byJob[3].status, byJob[3].ok], [0, 0]);               // timeout or network error = 0
  assert.ok(rows.every((r) => r.account_id === 1 && r.ran_at > 0 && r.ms >= 0));
  assert.ok(JSON.stringify(rows).indexOf("secret-body") < 0);
  assert.deepEqual(Object.keys(rows[0]).sort(), ["account_id", "id", "job_id", "ms", "ok", "ran_at", "status"]);
});

test("history API: owner only, newest first, limit defaults to 50 and is capped at 100", async () => {
  const e = env(), t = await addAccount(e), t2 = await addAccount(e, 2, "bob"); seed(e, 1, 1); seed(e, 2, 1);
  for (let i = 0; i < 120; i++) addRun(e, 1, 1, NOW + i * 1000, i % 2 ? 500 : 200);
  assert.equal((await call(e, "/api/jobs/1/runs")).status, 401);
  assert.equal((await call(e, "/api/jobs/1/runs", { token: t2 })).status, 404);      // someone else's cron
  assert.equal((await call(e, "/api/jobs/999/runs", { token: t })).status, 404);
  const get = async (q = "") => (await (await call(e, "/api/jobs/1/runs" + q, { token: t })).json()).runs;
  const def = await get();
  assert.equal(def.length, 50);
  assert.equal(def[0].ran_at, NOW + 119 * 1000); assert.ok(def[0].ran_at > def[1].ran_at);
  assert.deepEqual(def[0], { ran_at: NOW + 119000, status: 500, ms: 50, ok: false });
  assert.equal((await get("?limit=500")).length, 100);
  assert.equal((await get("?limit=3")).length, 3);
  assert.equal((await get("?limit=abc")).length, 50);
  assert.equal((await get("?limit=0")).length, 50);
  assert.equal((await (await call(e, "/api/jobs/2/runs", { token: t2 })).json()).runs.length, 0);
});

test("history: deleting a cron deletes its runs; deleting an account deletes all of its runs", async () => {
  const e = env(), t = await addAccount(e), t2 = await addAccount(e, 2, "bob"); seed(e, 1, 2); seed(e, 2, 1);
  addRun(e, 1, 1, NOW); addRun(e, 1, 1, NOW + 1); addRun(e, 2, 1, NOW); addRun(e, 3, 2, NOW);
  assert.equal((await call(e, "/api/jobs/1", { method: "DELETE", token: t2 })).status, 404);   // not bob's
  assert.equal(runsOf(e).length, 4);
  assert.equal((await call(e, "/api/jobs/1", { method: "DELETE", token: t })).status, 200);
  assert.deepEqual(runsOf(e).map((r) => r.job_id), [2, 3]);
  assert.equal((await call(e, "/api/me", { method: "DELETE", token: t })).status, 200);
  assert.deepEqual(runsOf(e).map((r) => [r.job_id, r.account_id]), [[3, 2]]);
});

test("history: retention is 7 days for free and 30 days for Premium, and each prune is bounded", async () => {
  const e = env(), T = Date.now(); await addAccount(e); await addAccount(e, 2, "bob", T + DAY); seed(e, 1, 1); seed(e, 2, 1);
  addRun(e, 1, 1, T - 8 * DAY); addRun(e, 1, 1, T - 6 * DAY);              // free: old one goes, recent stays
  addRun(e, 2, 2, T - 29 * DAY); addRun(e, 2, 2, T - 31 * DAY);            // premium: 29 days stays, 31 goes
  addRun(e, 9, 99, T - 1 * DAY); addRun(e, 9, 99, T - 8 * DAY);            // orphan (no account): follows the free rule
  assert.deepEqual(await pruneRuns(e, T), { pruned: 3 });
  assert.deepEqual(runsOf(e).map((r) => [r.account_id, Math.round((T - r.ran_at) / DAY)]).sort(), [[1, 6], [2, 29], [99, 1]]);
  const e2 = env(); await addAccount(e2); seed(e2, 1, 1);
  for (let i = 0; i < L.PRUNE_BATCH + 100; i++) addRun(e2, 1, 1, T - 20 * DAY);
  assert.equal((await pruneRuns(e2, T)).pruned, L.PRUNE_BATCH);
  assert.equal((await pruneRuns(e2, T)).pruned, 100);
  assert.equal(runsOf(e2).length, 0);
});

test("history: the runs table is created lazily when the migration did not run", async () => {
  const e = env(); await addAccount(e); seed(e, 1, 1);
  e.DB.sql.exec("DROP TABLE runs");
  assert.deepEqual(await runDue(e, NOW, okFetch(200)), { ran: 1, ok: 1, failed: 0 });
  assert.equal(runsOf(e).length, 1);
  const e2 = env(); const t = await addAccount(e2); seed(e2, 1, 1); e2.DB.sql.exec("DROP TABLE runs");
  assert.deepEqual((await (await call(e2, "/api/jobs/1/runs", { token: t })).json()).runs, []);
});

test("page: each cron has a History panel wired to the runs API, built without innerHTML", async () => {
  const html = await (await call(env(), "/")).text();
  assert.ok(html.includes('"/api/jobs/"+id+"/runs?limit=50"') && html.includes('"History"') && html.includes("loadRuns"));
  assert.ok(!html.includes("innerHTML") && !/style=/.test(html) && !/ on\w+=/.test(html));
});

// ---------- statistics ----------
test("stats API: login needed, range checked, 30d is Premium only", async () => {
  const e = env(), t = await addAccount(e), pt = await addAccount(e, 2, "bob", Date.now() + DAY);
  assert.equal((await call(e, "/api/stats")).status, 401);
  assert.equal((await call(e, "/api/stats?range=1y", { token: t })).status, 422);
  assert.equal((await call(e, "/api/stats?range=__proto__", { token: t })).status, 422);
  assert.equal((await call(e, "/api/stats?range=30d", { token: t })).status, 403);
  assert.equal((await call(e, "/api/stats?range=30d", { token: pt })).status, 200);
  for (const r of ["", "?range=24h", "?range=7d"]) assert.equal((await call(e, "/api/stats" + r, { token: t })).status, 200);
});

test("stats API: a brand-new account gets a full empty shape", async () => {
  const e = env(), t = await addAccount(e);
  const j = await (await call(e, "/api/stats?range=7d", { token: t })).json();
  assert.equal(j.range, "7d"); assert.equal(j.series.length, 7); assert.deepEqual(j.jobs, []);
  assert.deepEqual(j.totals, { runs: 0, ok: 0, failed: 0, success_rate: null, avg_ms: null });
});

test("stats API: totals, hourly and daily series and per-cron uptime come from SQL, for my account only", async () => {
  const e = env(), t = await addAccount(e), t2 = await addAccount(e, 2, "bob"); seed(e, 1, 2); seed(e, 2, 1);
  const T = Date.now(), HOUR = 3600000, h0 = Math.floor(T / HOUR) * HOUR;
  const run = (job, acct, at, status, ms) => e.DB.sql.prepare("INSERT INTO runs (job_id,account_id,ran_at,status,ms,ok) VALUES (?,?,?,?,?,?)").run(job, acct, at, status, ms, status >= 200 && status < 400 ? 1 : 0);
  run(1, 1, h0 + 1000, 200, 100); run(1, 1, h0 + 2000, 200, 300); run(1, 1, h0 + 3000, 500, 9000);   // this hour: avg of the 2 ok runs = 200
  run(2, 1, h0 - 2 * HOUR + 5, 200, 50); run(2, 1, h0 - 2 * HOUR + 9, 0, 10000);                      // two hours ago
  run(1, 1, h0 - 30 * HOUR, 200, 70);                                                                  // outside 24h, inside 7d
  run(3, 2, h0 + 1000, 200, 1);                                                                        // bob's run must never show up
  const h = await (await call(e, "/api/stats?range=24h", { token: t })).json();
  assert.deepEqual(h.totals, { runs: 5, ok: 3, failed: 2, success_rate: 60, avg_ms: 150 });          // avg over the ok runs only: (100 + 300 + 50) / 3
  assert.equal(h.series.length, 24); assert.equal(h.bucket_ms, HOUR);
  assert.deepEqual(h.series[23], { t: h0, runs: 3, ok: 2, failed: 1, avg_ms: 200 });
  assert.deepEqual(h.series[21], { t: h0 - 2 * HOUR, runs: 2, ok: 1, failed: 1, avg_ms: 50 });
  assert.deepEqual(h.series[22], { t: h0 - HOUR, runs: 0, ok: 0, failed: 0, avg_ms: null });
  assert.deepEqual(h.jobs.map((x) => [x.id, x.runs, x.ok, x.success_rate, x.avg_ms]), [[1, 3, 2, 66.7, 200], [2, 2, 1, 50, 50]]);
  const d = await (await call(e, "/api/stats?range=7d", { token: t })).json();                         // the 30-hour-old run joins in
  assert.equal(d.totals.runs, 6); assert.equal(d.series.length, 7); assert.equal(d.bucket_ms, 86400000);
  assert.equal(d.series.reduce((n, p) => n + p.runs, 0), 6);
  const b = await (await call(e, "/api/stats?range=24h", { token: t2 })).json();
  assert.deepEqual(b.totals, { runs: 1, ok: 1, failed: 0, success_rate: 100, avg_ms: 1 });
  assert.deepEqual(b.jobs.map((x) => x.id), [3]);
});

test("page: statistics view has range buttons, totals, charts and an empty state, drawn without a chart library", async () => {
  const html = await (await call(env(), "/")).text();
  for (const id of ["sRuns", "sRate", "sAvg", "sFailed", "cLine", "cBars", "sJobs", "sEmpty"]) assert.ok(html.includes(`id="${id}"`), id);
  for (const r of ["24h", "7d", "30d"]) assert.ok(html.includes(`data-range="${r}"`));
  assert.ok(html.includes("createElementNS") && html.includes('"/api/stats?range="'));
  assert.ok(!/<script[^>]+src="(?!https:\/\/challenges\.cloudflare\.com)/.test(html));   // no other external script
  assert.ok(!html.includes("innerHTML") && !/style=/.test(html) && !/ on\w+=/.test(html));
});

test("main page has no bot branding or links to the bot site or its Discord", async () => {
  const html = await (await call(env(), "/")).text();
  assert.ok(!/prime\s*bot|prime-bot|discord\.gg|pages\.dev/i.test(html));
  assert.ok(html.includes('href="/privacy"') && html.includes('href="/terms"'));
});

for (const p of ["privacy", "terms"]) {
  test(`/${p} returns 200 with a strict CSP, a nonce on the style tag and no bot branding`, async () => {
    const r = await call(env(), "/" + p), csp = r.headers.get("Content-Security-Policy"), html = await r.text();
    assert.equal(r.status, 200);
    assert.ok(csp.includes("default-src 'none'") && !csp.includes("script-src") && csp.includes("frame-ancestors 'none'"));
    const nonce = /'nonce-([^']+)'/.exec(csp)[1];
    for (const t of html.match(/<style[^>]*>/g)) assert.ok(t.includes(`nonce="${nonce}"`));
    assert.ok(!/<script/i.test(html) && !/\sstyle=|\son[a-z]+=/i.test(html));
    assert.ok(!/prime\s*bot|prime-bot|discord\.gg|pages\.dev/i.test(html));
  });
}

test("legal pages show a contact only when CONTACT_URL is a safe https or mailto link", async () => {
  assert.ok(!(await (await call(env(), "/terms")).text()).includes("Contact"));
  assert.ok(!(await (await call(env({ CONTACT_URL: "javascript:alert(1)" }), "/terms")).text()).includes("Contact"));
  const t = await (await call(env({ CONTACT_URL: "mailto:help@example.com" }), "/terms")).text();
  assert.ok(t.includes("Contact") && t.includes("help@example.com"));
});

test("legal pages only answer GET", async () => {
  assert.equal((await call(env(), "/privacy", { method: "POST", body: {} })).status, 404);
});

// ---- Step 3: public status pages ----
const addJob = (e, account_id, name, url = "https://secret.example.com/hook?key=SUPERSECRET", extra = {}) => {
  const r = e.DB.sql.prepare("INSERT INTO jobs (account_id, name, url, method, body, every_minutes, next_run_at, created_at) VALUES (?,?,?,?,?,?,?,0)").run(account_id, name, url, "GET", "", 15, 0);
  const id = Number(r.lastInsertRowid);
  if (extra.last_run_at) e.DB.sql.prepare("UPDATE jobs SET last_run_at = ?, last_status = ?, last_ms = 50 WHERE id = ?").run(extra.last_run_at, extra.last_status ?? 200, id);
  return id;
};
const addRunOk = (e, job_id, account_id, ran_at, ok) => e.DB.sql.prepare("INSERT INTO runs (job_id, account_id, ran_at, status, ms, ok) VALUES (?,?,?,?,?,?)").run(job_id, account_id, ran_at, ok ? 200 : 500, 40, ok ? 1 : 0);
const mkPage = async (e, tok, body = {}) => (await call(e, "/api/status-pages", { method: "POST", token: tok, body: { title: "My services", token: "cf", ...body } })).json();

test("status pages: sign-in is required to manage them, public routes need none", async () => {
  const e = env();
  assert.equal((await call(e, "/api/status-pages")).status, 401);
  assert.equal((await call(e, "/api/public/status/nothing-here")).status, 404);
  assert.equal((await call(e, "/s/nothing-here")).status, 404);
});

test("status pages: create with a random slug, rename, toggle, delete", async () => {
  const e = env(), tok = await addAccount(e);
  const c = await mkPage(e, tok);
  assert.match(c.slug, /^[a-f0-9]{20}$/);
  assert.equal((await call(e, `/api/status-pages/${c.id}`, { method: "PATCH", token: tok, body: { title: "Renamed", enabled: false } })).status, 200);
  const list = await (await call(e, "/api/status-pages", { token: tok })).json();
  assert.equal(list.pages[0].title, "Renamed"); assert.equal(list.pages[0].enabled, false); assert.equal(list.max_pages, 1);
  assert.equal((await call(e, `/api/status-pages/${c.id}`, { method: "DELETE", token: tok })).status, 200);
  assert.equal((await (await call(e, "/api/status-pages", { token: tok })).json()).pages.length, 0);
});

test("status pages: custom slug rules, reserved words and collisions", async () => {
  const e = env(), a = await addAccount(e, 1, "alice", Date.now() + 1e9), b = await addAccount(e, 2, "bob", Date.now() + 1e9);
  for (const bad of ["api", "s", "status", "privacy", "AB", "a--b", "-abc", "abc-", "has space", "x".repeat(41)]) {
    const r = await call(e, "/api/status-pages", { method: "POST", token: a, body: { title: "T", slug: bad, token: "cf" } });
    assert.equal(r.status, 422, bad);
  }
  assert.equal((await call(e, "/api/status-pages", { method: "POST", token: a, body: { title: "T", slug: "My-App", token: "cf" } })).status, 200);   // lowercased
  assert.equal((await call(e, "/api/status-pages", { method: "POST", token: b, body: { title: "T", slug: "my-app", token: "cf" } })).status, 409);
});

test("status pages: titles and labels reject links and empty text", async () => {
  const e = env(), tok = await addAccount(e, 1, "alice", Date.now() + 1e9);
  for (const t of ["", "   ", "visit https://evil.example", "go to www.evil.example"]) assert.equal((await call(e, "/api/status-pages", { method: "POST", token: tok, body: { title: t, token: "cf" } })).status, 422, t);
  assert.equal((await call(e, "/api/status-pages", { method: "POST", token: tok, body: { title: "x".repeat(61), token: "cf" } })).status, 422);
});

test("status pages: Turnstile is required to create", async () => {
  const e = env(), tok = await addAccount(e); nextOk = false;
  try { assert.equal((await call(e, "/api/status-pages", { method: "POST", token: tok, body: { title: "T", token: "bad" } })).status, 400); } finally { nextOk = true; }
});

test("status pages: free plan has 1 page and 5 monitors, Premium more", async () => {
  const e = env(), tok = await addAccount(e);
  const c = await mkPage(e, tok);
  assert.equal((await call(e, "/api/status-pages", { method: "POST", token: tok, body: { title: "Two", token: "cf" } })).status, 422);
  const ids = [1, 2, 3, 4, 5].map((i) => addJob(e, 1, "job" + i));
  for (const [i, id] of ids.entries()) assert.equal((await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: id, label: "L" + i } })).status, 200);
  const sixth = addJob(e, 1, "job6");
  assert.equal((await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: sixth, label: "L6" } })).status, 422);
  assert.equal((await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: ids[0], label: "Renamed" } })).status, 200);   // relabel still works at the limit
  const e2 = env(), p = await addAccount(e2, 1, "prem", Date.now() + 1e9);
  await mkPage(e2, p); assert.equal((await call(e2, "/api/status-pages", { method: "POST", token: p, body: { title: "Two", token: "cf" } })).status, 200);
});

test("status pages: only my own crons and my own pages (account isolation)", async () => {
  const e = env(), a = await addAccount(e, 1, "alice"), b = await addAccount(e, 2, "bob");
  const pa = await mkPage(e, a), jobB = addJob(e, 2, "bobs job");
  assert.equal((await call(e, `/api/status-pages/${pa.id}/monitors`, { method: "POST", token: a, body: { job_id: jobB, label: "steal" } })).status, 404);
  const jobA = addJob(e, 1, "mine");
  await call(e, `/api/status-pages/${pa.id}/monitors`, { method: "POST", token: a, body: { job_id: jobA, label: "Mine" } });
  assert.equal((await call(e, `/api/status-pages/${pa.id}`, { method: "PATCH", token: b, body: { title: "hijack" } })).status, 404);
  assert.equal((await call(e, `/api/status-pages/${pa.id}`, { method: "DELETE", token: b })).status, 404);
  assert.equal((await call(e, `/api/status-pages/${pa.id}/monitors/${jobA}`, { method: "DELETE", token: b })).status, 404);
  assert.equal((await (await call(e, "/api/status-pages", { token: b })).json()).pages.length, 0);
});

test("public status: shows label, state and uptime but never the URL, method, name or status text", async () => {
  const e = env(), tok = await addAccount(e), now = Date.now();
  const up = addJob(e, 1, "internal-name-ONE", undefined, { last_run_at: now - 60000, last_status: 200 }), down = addJob(e, 1, "internal-name-TWO", undefined, { last_run_at: now - 60000, last_status: 500 });
  for (let i = 0; i < 4; i++) { addRunOk(e, up, 1, now - i * 3600000, true); addRunOk(e, down, 1, now - i * 3600000, i === 0); }
  const c = await mkPage(e, tok, { slug: "my-status" });
  await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: up, label: "Website" } });
  await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: down, label: "API <b>x</b>" } });
  const jr = await call(e, "/api/public/status/my-status"), j = await jr.json(), raw = JSON.stringify(j);
  assert.equal(jr.status, 200);
  assert.equal(jr.headers.get("X-Robots-Tag"), "noindex, nofollow"); assert.equal(jr.headers.get("Cache-Control"), "public, max-age=60");
  assert.equal(j.monitors[0].state, "up"); assert.equal(j.monitors[0].uptime["24h"], 100);
  assert.equal(j.monitors[1].state, "down"); assert.equal(j.monitors[1].uptime["24h"], 25);
  assert.equal(j.overall, "degraded");
  const hr = await call(e, "/s/my-status"), html = await hr.text();
  assert.equal(hr.status, 200);
  for (const text of [raw, html]) for (const secret of ["secret.example.com", "SUPERSECRET", "internal-name", "https://secret", "GET"]) assert.ok(!text.includes(secret), secret);
  assert.ok(html.includes("API &lt;b&gt;x&lt;/b&gt;") && !html.includes("<b>x</b>"));
  assert.equal(hr.headers.get("X-Robots-Tag"), "noindex, nofollow");
  const csp = hr.headers.get("Content-Security-Policy"), nonce = /'nonce-([^']+)'/.exec(csp)[1];
  assert.ok(csp.includes("default-src 'none'") && !csp.includes("script-src") && csp.includes("frame-ancestors 'none'"));
  assert.ok(!/<script/i.test(html) && !/\sstyle=|\son[a-z]+=/i.test(html));
  for (const t of html.match(/<style[^>]*>/g)) assert.ok(t.includes(`nonce="${nonce}"`));
});

test("public status: free pages never show a 30-day number, Premium pages do", async () => {
  for (const [premium, expect30] of [[0, false], [Date.now() + 1e9, true]]) {
    const e = env(), tok = await addAccount(e, 1, "alice", premium), id = addJob(e, 1, "j", undefined, { last_run_at: Date.now(), last_status: 200 });
    addRunOk(e, id, 1, Date.now() - 1000, true);
    const c = await mkPage(e, tok, { slug: "pg-test" });
    await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: id, label: "A" } });
    const j = await (await call(e, "/api/public/status/pg-test")).json(), html = await (await call(e, "/s/pg-test")).text();
    assert.equal("30d" in j.monitors[0].uptime, expect30); assert.equal(j.windows.includes("30d"), expect30);
    assert.equal(html.includes("30d uptime"), expect30); assert.equal(html.includes("keeps 7 days"), !expect30);
  }
});

test("public status: paused and never-run crons are not reported as up or down", async () => {
  const e = env(), tok = await addAccount(e), fresh = addJob(e, 1, "fresh"), paused = addJob(e, 1, "paused", undefined, { last_run_at: 1, last_status: 200 });
  e.DB.sql.prepare("UPDATE jobs SET enabled = 0 WHERE id = ?").run(paused);
  const c = await mkPage(e, tok, { slug: "states" });
  for (const [id, l] of [[fresh, "Fresh"], [paused, "Paused"]]) await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: id, label: l } });
  const j = await (await call(e, "/api/public/status/states")).json();
  assert.deepEqual(j.monitors.map((m) => m.state), ["unknown", "paused"]); assert.equal(j.overall, "unknown");
});

test("public status: a disabled, blocked or deleted page returns 404 and the owner cannot undo a block", async () => {
  const e = env(), tok = await addAccount(e), c = await mkPage(e, tok, { slug: "gone-soon" });
  assert.equal((await call(e, "/s/gone-soon")).status, 200);
  await call(e, `/api/status-pages/${c.id}`, { method: "PATCH", token: tok, body: { enabled: false } });
  assert.equal((await call(e, "/s/gone-soon")).status, 404); assert.equal((await call(e, "/api/public/status/gone-soon")).status, 404);
  await call(e, `/api/status-pages/${c.id}`, { method: "PATCH", token: tok, body: { enabled: true } });
  assert.equal((await call(e, "/s/gone-soon")).status, 200);
  const adm = (blocked, key = "adminkey") => call(e, "/api/admin/status-page", { method: "POST", body: { slug: "gone-soon", blocked }, headers: { Authorization: "Bearer " + key } });
  assert.equal((await adm(true, "wrong")).status, 401);
  assert.equal((await adm(true)).status, 200);
  assert.equal((await call(e, "/s/gone-soon")).status, 404);
  assert.equal((await call(e, `/api/status-pages/${c.id}`, { method: "PATCH", token: tok, body: { enabled: true } })).status, 403);
  assert.equal((await call(e, "/s/gone-soon")).status, 404);
  await adm(false);
  assert.equal((await call(e, "/s/gone-soon")).status, 200);   // unblocked by the service owner: the owner's own enabled flag (true) applies again
});

test("status pages: deleting a cron removes it from pages, deleting the account removes pages", async () => {
  const e = env(), tok = await addAccount(e), id = addJob(e, 1, "j"), c = await mkPage(e, tok, { slug: "cascade" });
  await call(e, `/api/status-pages/${c.id}/monitors`, { method: "POST", token: tok, body: { job_id: id, label: "A" } });
  await call(e, `/api/jobs/${id}`, { method: "DELETE", token: tok });
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) AS n FROM status_page_jobs").get().n, 0);
  await call(e, "/api/me", { method: "DELETE", token: tok });
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) AS n FROM status_pages").get().n, 0);
  assert.equal((await call(e, "/s/cascade")).status, 404);
});

test("page: status view has the create form and page list, built without innerHTML, and no longer says coming soon", async () => {
  const html = await (await call(env(), "/")).text();
  for (const id of ["spTitle", "spSlug", "tsPage", "spAdd", "pages"]) assert.ok(html.includes(`id="${id}"`), id);
  assert.ok(html.includes('"/api/status-pages"') && html.includes("loadPages") && html.includes("Add to page"));
  const view = html.slice(html.indexOf('id="v-status"'), html.indexOf('id="v-stats"'));
  assert.ok(!view.includes("Coming soon"));
  assert.ok(!/Status pages<\/span><em class="chip soon"/.test(html));
  assert.ok(!html.includes("innerHTML") && !/style=/.test(html) && !/ on\w+=/.test(html));
});

// ---- Step 4: failure alerts (Discord webhook) ----
const HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyzABCDEF0123456789-_";
const putAlerts = (e, tok, body) => call(e, "/api/alerts", { method: "PUT", token: tok, body });
const failF = async () => new Response("no", { status: 500 });
const goodF = async () => new Response("ok", { status: 200 });
const dueJob = (e, acct = 1, name = "My cron") => {
  const id = addJob(e, acct, name, "https://example.com/p");
  e.DB.sql.prepare("UPDATE jobs SET next_run_at = 0 WHERE id = ?").run(id); return id;
};
const tick = async (e, id, f, now) => { e.DB.sql.prepare("UPDATE jobs SET next_run_at = 0 WHERE id = ?").run(id); return runDue(e, now, f); };
const outbox = (e) => e.DB.sql.prepare("SELECT kind, job_name, detail FROM alert_outbox ORDER BY id").all();

test("alerts API: login needed, only real Discord webhook addresses are accepted, the saved one is masked", async () => {
  const e = env(), tok = await addAccount(e);
  assert.equal((await call(e, "/api/alerts")).status, 401);
  for (const bad of ["https://evil.example/api/webhooks/123456789012345678/" + "a".repeat(40), "http://discord.com/api/webhooks/123456789012345678/" + "a".repeat(40),
    "https://discord.com.evil.example/api/webhooks/123456789012345678/" + "a".repeat(40), "https://discord.com/api/webhooks/abc/" + "a".repeat(40), "https://169.254.169.254/", 5])
    assert.equal((await putAlerts(e, tok, { webhook: bad })).status, 422, String(bad));
  const r = await putAlerts(e, tok, { webhook: HOOK }), j = await r.json();
  assert.equal(r.status, 200); assert.equal(j.enabled, true);
  assert.ok(j.webhook.endsWith("***6789") === false && j.webhook.endsWith("***89-_") === true);
  const raw = JSON.stringify(await (await call(e, "/api/alerts", { token: tok })).json());
  assert.ok(!raw.includes("abcdefghijklmnop") && raw.includes("123456789012345678"));
  assert.equal((await putAlerts(e, tok, { webhook: "" })).status, 200);
  assert.equal((await (await call(e, "/api/alerts", { token: tok })).json()).webhook, null);
  assert.ok(L.validDiscordWebhook(HOOK.replace("discord.com", "discordapp.com")));
});

test("alerts: test message is sent to the webhook, rate limited to one a minute, needs a saved webhook", async () => {
  const e = env(), tok = await addAccount(e), seen = [], real = globalThis.fetch;
  assert.equal((await call(e, "/api/alerts/test", { method: "POST", token: tok, body: {} })).status, 422);
  await putAlerts(e, tok, { webhook: HOOK });
  globalThis.fetch = async (u, init) => { if (String(u).includes("discord.com")) { seen.push([String(u), JSON.parse(init.body), init.redirect]); return new Response(null, { status: 204 }); } return real(u, init); };
  try {
    assert.equal((await call(e, "/api/alerts/test", { method: "POST", token: tok, body: {} })).status, 200);
    assert.equal((await call(e, "/api/alerts/test", { method: "POST", token: tok, body: {} })).status, 429);
  } finally { globalThis.fetch = real; }
  assert.equal(seen.length, 1); assert.equal(seen[0][0], HOOK); assert.equal(seen[0][2], "manual"); assert.deepEqual(seen[0][1].allowed_mentions, { parse: [] });
});

test("alerts: no alert without a webhook", async () => {
  const e = env(); await addAccount(e); const id = dueJob(e);
  for (let i = 0; i < 3; i++) await tick(e, id, failF, 1000 + i);
  assert.equal(outbox(e).length, 0);
});

test("alerts: one 'down' alert after 2 failures, nothing more while it keeps failing, one 'recovered' when it is back", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK }); const id = dueJob(e, 1, "Web *app*");
  await tick(e, id, failF, 1000); assert.equal(outbox(e).length, 0);                 // first failure: no alert yet
  await tick(e, id, failF, 2000); assert.deepEqual(outbox(e).map((o) => o.kind), ["down"]);
  await tick(e, id, failF, 3000); assert.equal(outbox(e).length, 1);                 // deduped
  await tick(e, id, goodF, 4000); assert.deepEqual(outbox(e).map((o) => o.kind), ["down", "recovered"]);
  await tick(e, id, goodF, 5000); assert.equal(outbox(e).length, 2);                   // healthy again: quiet
  assert.equal(e.DB.sql.prepare("SELECT alerting FROM jobs WHERE id = ?").get(id).alerting, 0);
  assert.equal(outbox(e)[0].detail, "HTTP 500");
});

test("alerts: after 5 failures the cron is switched off and a 'disabled' alert is queued", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK }); const id = dueJob(e);
  for (let i = 0; i < 5; i++) await tick(e, id, failF, 1000 * (i + 1));
  assert.deepEqual(outbox(e).map((o) => o.kind), ["down", "disabled"]);
  assert.equal(e.DB.sql.prepare("SELECT enabled FROM jobs WHERE id = ?").get(id).enabled, 0);
  await call(e, `/api/jobs/${id}`, { method: "PATCH", token: tok, body: { enabled: true } });   // turning it back on resets the alert state
  assert.equal(e.DB.sql.prepare("SELECT alerting FROM jobs WHERE id = ?").get(id).alerting, 0);
});

test("alerts: switched off by the user means nothing is queued", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK, enabled: false }); const id = dueJob(e);
  for (let i = 0; i < 3; i++) await tick(e, id, failF, 1000 * (i + 1));
  assert.equal(outbox(e).length, 0);
});

test("alerts: at most 20 per account per day", async () => {
  const e = env(), tok = await addAccount(e, 1, "alice", Date.now() + 1e9); await putAlerts(e, tok, { webhook: HOOK });
  for (let i = 0; i < 25; i++) {
    const id = dueJob(e, 1, "c" + i);
    await tick(e, id, failF, 1000 + i); await tick(e, id, failF, 2000 + i);
  }
  assert.equal(outbox(e).length, 20);
});

test("alerts: the outbox is sent in small batches, with no URL in the text, and cleaned up", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK });
  for (let i = 0; i < 5; i++) e.DB.sql.prepare("INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at) VALUES (1, ?, 'down', ?, 'HTTP 500', 1)").run(i, "job" + i);
  const sent = [], f = async (u, init) => { sent.push(JSON.parse(init.body).content); return new Response(null, { status: 204 }); };
  assert.equal((await sendAlerts(e, 5000, f)).sent, L.ALERT_SEND_BATCH);
  assert.equal(outbox(e).length, 5 - L.ALERT_SEND_BATCH);
  assert.ok(sent.every((t) => t.startsWith("FREE CRON:") && !t.includes("http")));
  await sendAlerts(e, 5000, f); assert.equal(outbox(e).length, 0);
});

test("alerts: a failing webhook is retried, then dropped, and never throws", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK });
  e.DB.sql.prepare("INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at) VALUES (1, 1, 'down', 'j', 'HTTP 500', 1)").run();
  const boom = async () => { throw new Error("network"); };
  await sendAlerts(e, 1, boom); await sendAlerts(e, 1, boom); assert.equal(outbox(e).length, 1);
  await sendAlerts(e, 1, boom); assert.equal(outbox(e).length, 0);
  e.DB.sql.prepare("INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at) VALUES (1, 1, 'down', 'j', 'x', 1)").run();
  await sendAlerts(e, 1, async () => new Response("gone", { status: 404 })); assert.equal(outbox(e).length, 0);   // webhook deleted on Discord's side
});

test("alerts: a webhook that breaks cannot break runDue (alerts are sent in a separate step)", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK }); const id = dueJob(e);
  const r = await runDue(e, 1000, failF);
  assert.equal(r.ran, 1); assert.equal(r.failed, 1);
});

test("alerts: text never contains the URL, mentions or markdown from the cron name; old outbox rows are pruned", async () => {
  const t = L.alertText("down", "@everyone `x` **hi**\nhttps://x", "HTTP 500");
  assert.ok(!t.includes("@") && !t.includes("`") && !t.includes("*") && !t.includes("\n"));
  const e = env(); await addAccount(e);
  e.DB.sql.prepare("INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at) VALUES (1, 1, 'down', 'j', 'x', 1)").run();
  await pruneOutbox(e, Date.now()); assert.equal(outbox(e).length, 0);
});

test("alerts: deleting a cron or the account removes their settings and queued alerts", async () => {
  const e = env(), tok = await addAccount(e); await putAlerts(e, tok, { webhook: HOOK }); const id = dueJob(e);
  e.DB.sql.prepare("INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at) VALUES (1, ?, 'down', 'j', 'x', 1)").run(id);
  await call(e, `/api/jobs/${id}`, { method: "DELETE", token: tok }); assert.equal(outbox(e).length, 0);
  await call(e, "/api/me", { method: "DELETE", token: tok });
  assert.equal(e.DB.sql.prepare("SELECT COUNT(*) AS n FROM account_settings").get().n, 0);
});

test("page: settings has the Discord alerts card and the module list shows alerts online", async () => {
  const html = await (await call(env(), "/")).text();
  for (const id of ["alHook", "alSave", "alTest", "alToggle", "alRemove", "alState"]) assert.ok(html.includes(`id="${id}"`), id);
  assert.ok(html.includes('"/api/alerts"') && html.includes('"/api/alerts/test"'));
  assert.ok(!/Failure alerts<span class="why">[^<]*<\/span><\/span><em class="chip soon"/.test(html));
  assert.ok(!html.includes("innerHTML") && !/style=/.test(html) && !/ on\w+=/.test(html));
});
