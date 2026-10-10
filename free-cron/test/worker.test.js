import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import worker, { runDue, recheckLicenses } from "../src/index.js";
import * as L from "../src/logic.js";

// A tiny D1 stand-in over node:sqlite (same SQL dialect, RETURNING included).
function makeDb() {
  const sql = new DatabaseSync(":memory:");
  for (const f of ["0001_init.sql", "0002_licenses.sql"]) sql.exec(readFileSync(new URL("../migrations/" + f, import.meta.url), "utf8"));
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
