// free-cron: a standalone free cron service. GitHub sign-in, Turnstile on sign-in and create, 5 free crons, Premium unlimited.
// One Cloudflare cron trigger (every 5 minutes) runs every user's due jobs; D1 holds accounts and jobs. No bot, no Discord.
import * as L from "./logic.js";
import { legalPage } from "./legal.js";
import { statusHtml, notFoundHtml } from "./status.js";
import { pageHtml } from "./page.js";

const UA = "free-cron/1.0 (scheduled HTTP request; report abuse via the service homepage)";
const SEC = { "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store" };

const json = (obj, status = 200, extra = {}) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...SEC, ...extra } });
const err = (status, message, extra) => json({ error: message }, status, extra);
const cookie = (name, value, { path = "/", maxAge = 0 } = {}) => `${name}=${value}; Path=${path}; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;

function selfHosts(env, url) { return [url?.hostname, ...String(env.SELF_HOSTS || "").split(",")].filter(Boolean); }

let licenseTableReady = false;
async function ensureLicenseTable(env) {            // belt and braces: works even if the migration step lacked D1:Edit
  if (licenseTableReady) return;
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS licenses (key_hash TEXT PRIMARY KEY, license_key TEXT NOT NULL, account_id INTEGER NOT NULL, recurring INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1, checked_at INTEGER NOT NULL, created_at INTEGER NOT NULL)").run();
  licenseTableReady = true;
}

const runsReady = new WeakSet();
async function ensureRunsTable(env) {               // same statements as migrations/0003_runs.sql; works even if the migration step lacked D1:Edit
  if (runsReady.has(env.DB)) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, account_id INTEGER NOT NULL, ran_at INTEGER NOT NULL, status INTEGER NOT NULL, ms INTEGER NOT NULL, ok INTEGER NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS runs_job_idx ON runs (job_id, ran_at)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS runs_acct_idx ON runs (account_id, ran_at)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS runs_time_idx ON runs (ran_at)"),
  ]);
  runsReady.add(env.DB);
}

const statusReady = new WeakSet();
async function ensureStatusTables(env) {            // same statements as migrations/0004_status_pages.sql; works even if the migration step lacked D1:Edit
  if (statusReady.has(env.DB)) return;
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS status_pages (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, blocked INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS status_pages_acct_idx ON status_pages (account_id)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS status_page_jobs (page_id INTEGER NOT NULL, job_id INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (page_id, job_id))"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS status_page_jobs_job_idx ON status_page_jobs (job_id)"),
  ]);
  statusReady.add(env.DB);
}

/** Shared per-account creation budget (20 / 24 h), counted atomically so deleting and re-creating can't dodge it. */
async function takeCreateBudget(env, account, now) {
  const r = await env.DB.prepare(
    `UPDATE accounts SET create_count = CASE WHEN create_window_start < ?2 THEN 1 ELSE create_count + 1 END,
                         create_window_start = CASE WHEN create_window_start < ?2 THEN ?3 ELSE create_window_start END
     WHERE id = ?1 AND (create_window_start < ?2 OR create_count < ?4)`).bind(account.id, now - 86400000, now, L.CREATES_PER_DAY).run();
  return !!r.meta.changes;
}

/** What a status page may show publicly. Returns null for a missing, switched-off or blocked page (all look the same from outside). */
async function publicStatus(env, slug, now = Date.now()) {
  if (!L.validSlug(slug) && !/^[a-z0-9]{20}$/.test(slug)) return null;
  await ensureStatusTables(env); await ensureRunsTable(env);
  const page = await env.DB.prepare("SELECT p.id, p.title, p.account_id, a.premium_until FROM status_pages p JOIN accounts a ON a.id = p.account_id WHERE p.slug = ?1 AND p.enabled = 1 AND p.blocked = 0").bind(slug).first();
  if (!page) return null;
  const premium = L.isPremium({ premium_until: page.premium_until }, now);
  const [mons, use] = await Promise.all([
    env.DB.prepare("SELECT pj.job_id, pj.label, j.enabled, j.last_run_at, j.last_status FROM status_page_jobs pj JOIN jobs j ON j.id = pj.job_id AND j.account_id = ?2 WHERE pj.page_id = ?1 ORDER BY pj.rowid").bind(page.id, page.account_id).all(),
    env.DB.prepare(`SELECT r.job_id,
        SUM(CASE WHEN r.ran_at >= ?2 THEN 1 ELSE 0 END) AS "n_24h", SUM(CASE WHEN r.ran_at >= ?2 THEN r.ok ELSE 0 END) AS "ok_24h",
        SUM(CASE WHEN r.ran_at >= ?3 THEN 1 ELSE 0 END) AS "n_7d",  SUM(CASE WHEN r.ran_at >= ?3 THEN r.ok ELSE 0 END) AS "ok_7d",
        COUNT(*) AS "n_30d", SUM(r.ok) AS "ok_30d"
      FROM runs r JOIN status_page_jobs pj ON pj.job_id = r.job_id WHERE pj.page_id = ?1 AND r.ran_at >= ?4 GROUP BY r.job_id`).bind(page.id, now - 86400000, now - 7 * 86400000, now - 30 * 86400000).all(),
  ]);
  return L.buildPublicStatus(page, mons.results || [], use.results || [], premium, now);
}

const alertsReady = new WeakSet();
async function ensureAlertTables(env) {            // same as migrations/0005_alerts.sql; works even if the migration step lacked D1:Edit
  if (alertsReady.has(env.DB)) return;
  const cols = ((await env.DB.prepare("PRAGMA table_info(jobs)").all()).results || []).map((c) => c.name);
  const alter = [];
  if (!cols.includes("alerting")) alter.push(env.DB.prepare("ALTER TABLE jobs ADD COLUMN alerting INTEGER NOT NULL DEFAULT 0"));   // ALTER has no IF NOT EXISTS, hence the check above
  if (!cols.includes("last_alert_at")) alter.push(env.DB.prepare("ALTER TABLE jobs ADD COLUMN last_alert_at INTEGER"));
  try { if (alter.length) await env.DB.batch(alter); } catch { /* a parallel run added them first */ }
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS account_settings (account_id INTEGER PRIMARY KEY, discord_webhook TEXT, alerts_enabled INTEGER NOT NULL DEFAULT 1, alert_window_start INTEGER NOT NULL DEFAULT 0, alert_count INTEGER NOT NULL DEFAULT 0, last_test_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS alert_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, job_id INTEGER NOT NULL, kind TEXT NOT NULL, job_name TEXT NOT NULL, detail TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS alert_outbox_acct_idx ON alert_outbox (account_id)"),
  ]);
  alertsReady.add(env.DB);
}

/** POST one message to a Discord webhook. Returns the HTTP status, or 0 on a network error or timeout. Redirects are not followed. */
async function postDiscord(url, text, doFetch = fetch) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), L.ALERT_TIMEOUT_MS);
  try {
    const r = await doFetch(url, { method: "POST", redirect: "manual", signal: ctl.signal, headers: { "Content-Type": "application/json", "User-Agent": UA },
      body: JSON.stringify({ content: text, allowed_mentions: { parse: [] } }) });
    try { await r.body?.cancel(); } catch { /* nothing to read */ }
    return r.status;
  } catch { return 0; } finally { clearTimeout(timer); }
}

/** Send a few queued alerts. Runs after runDue() in its own step, so a failing webhook can never break a tick. */
export async function sendAlerts(env, now = Date.now(), doFetch = fetch) {
  if (String(env.DISABLED || "") === "1") return { skipped: "disabled" };
  await ensureAlertTables(env);
  const rows = ((await env.DB.prepare(
    `SELECT o.id, o.kind, o.job_name, o.detail, o.attempts, s.discord_webhook FROM alert_outbox o
     JOIN account_settings s ON s.account_id = o.account_id
     WHERE s.alerts_enabled = 1 AND s.discord_webhook IS NOT NULL ORDER BY o.id LIMIT ?1`).bind(L.ALERT_SEND_BATCH).all()).results) || [];
  if (!rows.length) return { sent: 0 };
  const stmts = []; let sent = 0;
  await Promise.all(rows.map(async (r) => {
    const st = await postDiscord(r.discord_webhook, L.alertText(r.kind, r.job_name, r.detail), doFetch);
    if (st >= 200 && st < 300) { sent++; stmts.push(env.DB.prepare("DELETE FROM alert_outbox WHERE id = ?1").bind(r.id)); }
    else if (st === 401 || st === 403 || st === 404 || r.attempts + 1 >= L.ALERT_MAX_TRIES) stmts.push(env.DB.prepare("DELETE FROM alert_outbox WHERE id = ?1").bind(r.id));   // webhook gone, or tried enough
    else stmts.push(env.DB.prepare("UPDATE alert_outbox SET attempts = attempts + 1 WHERE id = ?1").bind(r.id));
  }));
  await env.DB.batch(stmts);
  return { sent };
}
export async function pruneOutbox(env, now = Date.now()) {
  await ensureAlertTables(env);
  const r = await env.DB.prepare("DELETE FROM alert_outbox WHERE created_at < ?1").bind(now - L.ALERT_KEEP_MS).run();
  return { pruned: r.meta.changes };
}

/** Remove old run rows: 7 days for free accounts, 30 for Premium. Bounded, so one call is cheap. */
export async function pruneRuns(env, now = Date.now()) {
  await ensureRunsTable(env);
  const r = await env.DB.prepare(
    `DELETE FROM runs WHERE id IN (
       SELECT r.id FROM runs r LEFT JOIN accounts a ON a.id = r.account_id
       WHERE r.ran_at < ?2 AND (r.ran_at < ?3 OR a.id IS NULL OR a.premium_until <= ?1)
       LIMIT ?4)`).bind(now, now - L.KEEP_DAYS_FREE * 86400000, now - L.KEEP_DAYS_PREMIUM * 86400000, L.PRUNE_BATCH).run();
  return { pruned: r.meta.changes };
}

/** Ask Gumroad about one license key. Never increments the use counter. Returns {resp} or {net:true}. */
async function gumroadVerify(env, key, doFetch = fetch) {
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), L.TIMEOUT_MS);
    const r = await doFetch("https://api.gumroad.com/v2/licenses/verify", {
      method: "POST", redirect: "manual", signal: ctl.signal, headers: { "User-Agent": UA, Accept: "application/json" },
      body: new URLSearchParams({ product_id: env.GUMROAD_PRODUCT_ID, license_key: key, increment_uses_count: "false" }),
    });
    clearTimeout(timer);
    if (r.status >= 500 || r.status === 429) return { net: true };
    const resp = await r.json().catch(() => null);
    return resp ? { resp } : { net: true };
  } catch { return { net: true }; }
}

async function turnstileOk(env, token, request) {
  if (!env.TURNSTILE_SECRET) return false;                       // fail closed
  if (typeof token !== "string" || !token || token.length > 2048) return false;
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip) form.append("remoteip", ip);
  try {
    const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", { method: "POST", body: form });
    return !!(await r.json()).success;
  } catch { return false; }
}

async function sessionOf(request, env) {
  const raw = L.parseCookies(request.headers.get("Cookie")).fc_session;
  const p = await L.verifyToken(raw, env.SESSION_SECRET, Date.now());
  if (!p) return null;
  return env.DB.prepare("SELECT * FROM accounts WHERE id = ?1").bind(p.id).first();
}

const jobView = (j, account, now, index) => ({
  id: j.id, name: j.name, url: j.url, method: j.method, every_minutes: j.every_minutes, enabled: !!j.enabled,
  next_run_at: j.next_run_at, last_run_at: j.last_run_at, last_status: j.last_status, last_ms: j.last_ms, fail_count: j.fail_count,
  over_limit: !L.isPremium(account, now) && index >= L.FREE_LIMIT,
});

async function me(env, account) {
  const now = Date.now();
  const rows = (await env.DB.prepare("SELECT * FROM jobs WHERE account_id = ?1 ORDER BY id").bind(account.id).all()).results || [];
  const lim = L.limitFor(account, now);
  const jobs = rows.map((j, i) => jobView(j, account, now, i));
  return {
    buy_url: L.safeBuyUrl(env.GUMROAD_BUY_URL), login: account.login, premium: L.isPremium(account, now), premium_until: L.isPremium(account, now) ? account.premium_until : null,
    limit: lim === Infinity ? null : lim, used: rows.length, min_minutes: L.minMinutesFor(account, now), intervals: L.INTERVALS,
    jobs, stats: L.tally(jobs),
  };
}

async function handleAlerts(request, env, url, account) {
  const path = url.pathname, method = request.method, now = Date.now();
  await ensureAlertTables(env);
  const view = async () => {
    const r = await env.DB.prepare("SELECT discord_webhook, alerts_enabled FROM account_settings WHERE account_id = ?1").bind(account.id).first();
    return { webhook: L.maskWebhook(r?.discord_webhook), enabled: r ? !!r.alerts_enabled : true, after: L.ALERT_AFTER, disable_after: L.MAX_FAILS };
  };
  if (path === "/api/alerts" && method === "GET") return json(await view());
  if (path === "/api/alerts" && method === "PUT") {
    const b = await request.json().catch(() => ({}));
    if (b.webhook === undefined && b.enabled === undefined) return err(422, "Send a webhook or enabled.");
    if (b.enabled !== undefined && typeof b.enabled !== "boolean") return err(422, "enabled must be true or false.");
    let hook;                                                            // undefined = keep, null = remove, string = set
    if (b.webhook === null || b.webhook === "") hook = null;
    else if (b.webhook !== undefined) { hook = L.validDiscordWebhook(b.webhook); if (!hook) return err(422, "That isn't a Discord webhook address (https://discord.com/api/webhooks/...)."); }
    await env.DB.prepare("INSERT INTO account_settings (account_id, updated_at) VALUES (?1, ?2) ON CONFLICT(account_id) DO NOTHING").bind(account.id, now).run();
    await env.DB.prepare("UPDATE account_settings SET discord_webhook = CASE WHEN ?2 THEN ?3 ELSE discord_webhook END, alerts_enabled = COALESCE(?4, alerts_enabled), updated_at = ?5 WHERE account_id = ?1")
      .bind(account.id, hook === undefined ? 0 : 1, hook ?? null, b.enabled === undefined ? null : b.enabled ? 1 : 0, now).run();
    if (hook === null) await env.DB.prepare("DELETE FROM alert_outbox WHERE account_id = ?1").bind(account.id).run();
    return json(await view());
  }
  if (path === "/api/alerts/test" && method === "POST") {                 // one real message, at most once a minute per account
    const row = await env.DB.prepare("SELECT discord_webhook FROM account_settings WHERE account_id = ?1").bind(account.id).first();
    if (!row?.discord_webhook) return err(422, "Save a Discord webhook first.");
    const slot = await env.DB.prepare("UPDATE account_settings SET last_test_at = ?2 WHERE account_id = ?1 AND last_test_at < ?3").bind(account.id, now, now - 60000).run();
    if (!slot.meta.changes) return err(429, "Please wait a minute before sending another test.");
    const st = await postDiscord(row.discord_webhook, L.alertText("test"));
    return st >= 200 && st < 300 ? json({ sent: true }) : err(502, "Discord didn't accept the message. Check the webhook address.");
  }
  return err(404, "Not found.");
}

async function pagesView(env, account) {
  await ensureStatusTables(env);
  const now = Date.now();
  const [pg, mo] = await Promise.all([
    env.DB.prepare("SELECT id, slug, title, enabled, blocked FROM status_pages WHERE account_id = ?1 ORDER BY id").bind(account.id).all(),
    env.DB.prepare("SELECT pj.page_id, pj.job_id, pj.label FROM status_page_jobs pj JOIN status_pages p ON p.id = pj.page_id WHERE p.account_id = ?1 ORDER BY pj.rowid").bind(account.id).all(),
  ]);
  const by = new Map();
  for (const m of mo.results || []) { if (!by.has(m.page_id)) by.set(m.page_id, []); by.get(m.page_id).push({ job_id: m.job_id, label: m.label }); }
  return { pages: (pg.results || []).map((p) => ({ id: p.id, slug: p.slug, title: p.title, enabled: !!p.enabled, blocked: !!p.blocked, path: "/s/" + p.slug, monitors: by.get(p.id) || [] })),
    max_pages: L.statusPagesLimit(account, now), max_monitors: L.statusMonitorsLimit(account, now) };
}

async function handleStatusPages(request, env, url, account) {
  const path = url.pathname, method = request.method, now = Date.now();
  await ensureStatusTables(env);
  if (path === "/api/status-pages" && method === "GET") return json(await pagesView(env, account));
  if (path === "/api/status-pages" && method === "POST") {
    const b = await request.json().catch(() => ({}));
    if (!(await turnstileOk(env, b.token, request))) return err(400, "Please complete the captcha and try again.");
    const t = L.cleanText(b.title, "Title");
    if (t.error) return err(422, t.error);
    let slug = L.randomSlug();
    if (b.slug !== undefined && b.slug !== null && b.slug !== "") {
      slug = String(b.slug).trim().toLowerCase();
      if (!L.validSlug(slug)) return err(422, "The address must be 3 to 40 lowercase letters, digits or single dashes, and can't be a reserved word.");
    }
    if (!(await takeCreateBudget(env, account, now))) return err(429, "You've created a lot today. Try again tomorrow.");
    let r;
    try {
      r = await env.DB.prepare("INSERT INTO status_pages (account_id, slug, title, created_at) SELECT ?1, ?2, ?3, ?4 WHERE (SELECT COUNT(*) FROM status_pages WHERE account_id = ?1) < ?5")
        .bind(account.id, slug, t.value, now, L.statusPagesLimit(account, now)).run();
    } catch { return err(409, "That address is taken. Pick another."); }
    if (!r.meta.changes) return err(422, `You can have ${L.statusPagesLimit(account, now)} status ${L.statusPagesLimit(account, now) === 1 ? "page" : "pages"} on this plan.`);
    return json({ id: r.meta.last_row_id, slug });
  }
  const m = path.match(/^\/api\/status-pages\/(\d{1,12})(?:\/monitors(?:\/(\d{1,12}))?)?$/);
  if (!m) return err(404, "Not found.");
  const pid = Number(m[1]), isMon = path.includes("/monitors");
  const page = await env.DB.prepare("SELECT id, blocked FROM status_pages WHERE id = ?1 AND account_id = ?2").bind(pid, account.id).first();
  if (!page) return err(404, "That status page doesn't exist.");
  if (!isMon && method === "DELETE") {
    await env.DB.batch([env.DB.prepare("DELETE FROM status_page_jobs WHERE page_id = ?1").bind(pid), env.DB.prepare("DELETE FROM status_pages WHERE id = ?1 AND account_id = ?2").bind(pid, account.id)]);
    return json({ deleted: true });
  }
  if (!isMon && method === "PATCH") {
    const b = await request.json().catch(() => ({}));
    if (b.title === undefined && b.enabled === undefined) return err(422, "Send a title or enabled.");
    if (b.enabled !== undefined && typeof b.enabled !== "boolean") return err(422, "enabled must be true or false.");
    if (b.enabled === true && page.blocked) return err(403, "This page was switched off by the service and can't be turned on.");
    let title = null;
    if (b.title !== undefined) { const t = L.cleanText(b.title, "Title"); if (t.error) return err(422, t.error); title = t.value; }
    await env.DB.prepare("UPDATE status_pages SET title = COALESCE(?3, title), enabled = COALESCE(?4, enabled) WHERE id = ?1 AND account_id = ?2")
      .bind(pid, account.id, title, b.enabled === undefined ? null : b.enabled ? 1 : 0).run();
    return json({ updated: true });
  }
  if (isMon && method === "POST" && !m[2]) {                              // add a cron (or relabel one already on the page)
    const b = await request.json().catch(() => ({}));
    const jid = Number(b.job_id);
    if (!Number.isInteger(jid) || jid < 1) return err(422, "Pick a cron.");
    const t = L.cleanText(b.label, "Label");
    if (t.error) return err(422, t.error);
    if (!(await env.DB.prepare("SELECT id FROM jobs WHERE id = ?1 AND account_id = ?2").bind(jid, account.id).first())) return err(404, "That cron doesn't exist.");
    const up = await env.DB.prepare("UPDATE status_page_jobs SET label = ?3 WHERE page_id = ?1 AND job_id = ?2").bind(pid, jid, t.value).run();
    if (up.meta.changes) return json({ updated: true });
    const max = L.statusMonitorsLimit(account, now);
    const ins = await env.DB.prepare("INSERT INTO status_page_jobs (page_id, job_id, label) SELECT ?1, ?2, ?3 WHERE (SELECT COUNT(*) FROM status_page_jobs WHERE page_id = ?1) < ?4").bind(pid, jid, t.value, max).run();
    return ins.meta.changes ? json({ added: true }) : err(422, `A page can show ${max} crons on this plan.`);
  }
  if (isMon && method === "DELETE" && m[2]) {
    const r = await env.DB.prepare("DELETE FROM status_page_jobs WHERE page_id = ?1 AND job_id = ?2").bind(pid, Number(m[2])).run();
    return r.meta.changes ? json({ deleted: true }) : err(404, "That cron isn't on this page.");
  }
  return err(404, "Not found.");
}

async function handleApi(request, env, url) {
  const path = url.pathname, method = request.method;
  if (method !== "GET" && !L.sameOriginOk(request, url)) return err(403, "Blocked: cross-site request.");

  if (path === "/api/login" && method === "POST") {
    const body = await request.json().catch(() => ({}));
    if (!(await turnstileOk(env, body.token, request))) return err(400, "Please complete the captcha and try again.");
    if (!env.GITHUB_CLIENT_ID || !env.GITHUB_CLIENT_SECRET || !env.SESSION_SECRET) return err(503, "Sign-in isn't set up yet.");
    const state = L.randomId();
    const auth = "https://github.com/login/oauth/authorize?" + new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, redirect_uri: `${url.origin}/auth/callback`, state, allow_signup: "true" });
    return json({ url: auth }, 200, { "Set-Cookie": cookie("fc_state", state, { path: "/auth", maxAge: 600 }) });
  }
  if (path === "/api/logout" && method === "POST") return json({ ok: true }, 200, { "Set-Cookie": cookie("fc_session", "", {}) });

  if (path === "/api/admin/premium" && method === "POST") {              // owner only: grant/extend/revoke Premium by GitHub login
    if (!env.ADMIN_KEY) return err(404, "Not found.");
    if (!L.safeEqual(request.headers.get("Authorization") || "", `Bearer ${env.ADMIN_KEY}`)) return err(401, "Unauthorized.");
    const b = await request.json().catch(() => ({}));
    const days = Number(b.days);
    if (typeof b.login !== "string" || !/^[A-Za-z0-9-]{1,39}$/.test(b.login) || !Number.isInteger(days) || days < 0 || days > 3650) return err(422, "login and days (0-3650) are required.");
    const now = Date.now();
    const until = days === 0 ? 0 : now + days * 86400000;
    const r = await env.DB.prepare("UPDATE accounts SET premium_until = ?2 WHERE login = ?1 COLLATE NOCASE").bind(b.login, until).run();
    return json({ updated: r.meta.changes || 0, premium_until: until });
  }

  if (path === "/api/admin/status-page" && method === "POST") {         // owner only: block or unblock a status page by slug (abuse)
    if (!env.ADMIN_KEY) return err(404, "Not found.");
    if (!L.safeEqual(request.headers.get("Authorization") || "", `Bearer ${env.ADMIN_KEY}`)) return err(401, "Unauthorized.");
    const b = await request.json().catch(() => ({}));
    if (typeof b.slug !== "string" || !/^[a-z0-9-]{3,40}$/.test(b.slug) || typeof b.blocked !== "boolean") return err(422, "slug and blocked (true or false) are required.");
    await ensureStatusTables(env);
    const r = await env.DB.prepare("UPDATE status_pages SET blocked = ?2 WHERE slug = ?1").bind(b.slug, b.blocked ? 1 : 0).run();
    return json({ updated: r.meta.changes || 0 });
  }

  const pm = path.match(/^\/api\/public\/status\/([a-z0-9-]{3,40})$/);
  if (pm && method === "GET") {                                          // public, no login: label, state, uptime, last checked. Never a URL.
    const data = await publicStatus(env, pm[1]);
    const pub = { "X-Robots-Tag": "noindex, nofollow" };
    return data ? json(data, 200, { ...pub, "Cache-Control": "public, max-age=60" }) : err(404, "Not found.", pub);
  }

  const account = await sessionOf(request, env);
  if (!account) return err(401, "Please sign in.");

  if (path === "/api/me" && method === "GET") return json(await me(env, account));
  if (path === "/api/me" && method === "DELETE") {                       // delete my account and every cron
    await ensureLicenseTable(env); await ensureRunsTable(env); await ensureStatusTables(env); await ensureAlertTables(env);
    await env.DB.batch([env.DB.prepare("DELETE FROM runs WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM jobs WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM licenses WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM status_page_jobs WHERE page_id IN (SELECT id FROM status_pages WHERE account_id = ?1)").bind(account.id), env.DB.prepare("DELETE FROM status_pages WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM account_settings WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM alert_outbox WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM accounts WHERE id = ?1").bind(account.id)]);
    return json({ deleted: true }, 200, { "Set-Cookie": cookie("fc_session", "", {}) });
  }

  if (path === "/api/redeem" && method === "POST") {                      // paste a Gumroad license key -> Premium, automatically
    const body = await request.json().catch(() => ({}));
    if (!env.GUMROAD_PRODUCT_ID) return err(503, "Premium isn't set up yet.");
    if (!(await turnstileOk(env, body.token, request))) return err(400, "Please complete the captcha and try again.");
    if (!L.validKey(body.key)) return err(422, "That doesn't look like a license key.");
    const key = body.key.trim(), now = Date.now();
    const g = await gumroadVerify(env, key);
    if (g.net) return err(502, "Couldn't reach Gumroad. Please try again in a minute.");
    const verdict = L.judgeLicense(g.resp, env.GUMROAD_PRODUCT_ID);
    if (!verdict.ok) return err(422, verdict.error);
    await ensureLicenseTable(env);
    const hash = await L.sha256Hex(key), recurring = L.isRecurring(env) ? 1 : 0;
    const ins = await env.DB.prepare("INSERT INTO licenses (key_hash, license_key, account_id, recurring, checked_at, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?5) ON CONFLICT(key_hash) DO NOTHING")
      .bind(hash, key, account.id, recurring, now).run();
    const row = await env.DB.prepare("SELECT account_id, recurring, active FROM licenses WHERE key_hash = ?1").bind(hash).first();
    if (!row || row.account_id !== account.id) return err(409, "That license key is already used by another account.");
    if (!row.active) return err(422, "That license key is no longer active.");
    let until = 0;
    if (row.recurring) until = now + L.GRACE_DAYS * 86400000;                // renewed by the daily re-check
    else if (ins.meta.changes) until = now + L.daysFor(env) * 86400000;      // one-time purchase: fixed length, granted once
    else return json({ already: true });
    if (until) await env.DB.prepare("UPDATE accounts SET premium_until = MAX(premium_until, ?2) WHERE id = ?1").bind(account.id, until).run();
    return json({ premium: true });
  }

  if (path === "/api/jobs" && method === "POST") {
    const body = await request.json().catch(() => ({}));
    const now = Date.now();
    if (!(await turnstileOk(env, body.token, request))) return err(400, "Please complete the captcha and try again.");
    const v = L.validateJob(body, account, now, selfHosts(env, url));
    if (v.error) return err(422, v.error);
    if (!(await takeCreateBudget(env, account, now))) return err(429, "You've created a lot of crons today. Try again tomorrow.");
    const s = v.spec, lim = L.limitFor(account, now);
    const ins = await env.DB.prepare(
      `INSERT INTO jobs (account_id, name, url, method, body, every_minutes, next_run_at, created_at)
       SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8 WHERE (SELECT COUNT(*) FROM jobs WHERE account_id = ?1) < ?9`)
      .bind(account.id, s.name, s.url, s.method, s.body, s.every_minutes, L.nextRunAt(now, s.every_minutes, account), now, lim === Infinity ? 1e9 : lim).run();
    if (!ins.meta.changes) return err(422, `You can have ${L.FREE_LIMIT} free crons. Delete one or upgrade to Premium.`);
    return json({ id: ins.meta.last_row_id });
  }

  if (path === "/api/stats" && method === "GET") {                         // totals, a time series and per-cron uptime, all aggregated in SQL
    const now = Date.now(), range = url.searchParams.get("range") || "24h", w = L.statsWindow(range, now);
    if (!w) return err(422, "range must be 24h, 7d or 30d.");
    if (range === "30d" && !L.isPremium(account, now)) return err(403, "30 days of statistics is a Premium feature.");
    await ensureRunsTable(env);
    const q = (sql, ...args) => env.DB.prepare(sql).bind(...args).all();
    const [t, s, j] = await Promise.all([
      env.DB.prepare("SELECT COUNT(*) AS runs, SUM(ok) AS ok, AVG(CASE WHEN ok = 1 THEN ms END) AS avg_ms FROM runs WHERE account_id = ?1 AND ran_at >= ?2").bind(account.id, w.from).first(),
      q("SELECT CAST(ran_at / CAST(?3 AS INTEGER) AS INTEGER) * CAST(?3 AS INTEGER) AS b, COUNT(*) AS runs, SUM(ok) AS ok, AVG(CASE WHEN ok = 1 THEN ms END) AS avg_ms FROM runs WHERE account_id = ?1 AND ran_at >= ?2 GROUP BY b", account.id, w.from, w.size),
      q("SELECT j.id, j.name, COUNT(r.id) AS runs, SUM(r.ok) AS ok, AVG(CASE WHEN r.ok = 1 THEN r.ms END) AS avg_ms FROM jobs j LEFT JOIN runs r ON r.job_id = j.id AND r.ran_at >= ?2 WHERE j.account_id = ?1 GROUP BY j.id ORDER BY j.id", account.id, w.from),
    ]);
    return json(L.buildStats(range, now, t, s.results || [], j.results || []));
  }

  if (path.startsWith("/api/status-pages")) return handleStatusPages(request, env, url, account);
  if (path.startsWith("/api/alerts")) return handleAlerts(request, env, url, account);

  const hm = path.match(/^\/api\/jobs\/(\d{1,12})\/runs$/);
  if (hm && method === "GET") {                                            // run history of one of MY crons, newest first
    const id = Number(hm[1]);
    if (!(await env.DB.prepare("SELECT id FROM jobs WHERE id = ?1 AND account_id = ?2").bind(id, account.id).first())) return err(404, "That cron doesn't exist.");
    await ensureRunsTable(env);
    const rows = (await env.DB.prepare("SELECT ran_at, status, ms, ok FROM runs WHERE job_id = ?1 AND account_id = ?2 ORDER BY ran_at DESC, id DESC LIMIT ?3")
      .bind(id, account.id, L.runsLimit(url.searchParams.get("limit"))).all()).results || [];
    return json({ runs: rows.map((r) => ({ ran_at: r.ran_at, status: r.status, ms: r.ms, ok: !!r.ok })) });
  }

  const m = path.match(/^\/api\/jobs\/(\d{1,12})$/);
  if (m) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      await ensureRunsTable(env);
      const r = await env.DB.prepare("DELETE FROM jobs WHERE id = ?1 AND account_id = ?2").bind(id, account.id).run();
      if (!r.meta.changes) return err(404, "That cron doesn't exist.");
      await env.DB.prepare("DELETE FROM runs WHERE job_id = ?1 AND account_id = ?2").bind(id, account.id).run();   // its history goes with it
      await ensureAlertTables(env); await ensureStatusTables(env); await env.DB.batch([env.DB.prepare("DELETE FROM status_page_jobs WHERE job_id = ?1").bind(id), env.DB.prepare("DELETE FROM alert_outbox WHERE job_id = ?1").bind(id)]);   // and it leaves any status page
      return json({ deleted: true });
    }
    if (method === "PATCH") {
      await ensureAlertTables(env);
      const b = await request.json().catch(() => ({}));
      if (typeof b.enabled !== "boolean") return err(422, "enabled must be true or false.");
      const now = Date.now();
      const r = b.enabled
        ? await env.DB.prepare("UPDATE jobs SET enabled = 1, fail_count = 0, alerting = 0, next_run_at = ?3 WHERE id = ?1 AND account_id = ?2").bind(id, account.id, now + 60000).run()
        : await env.DB.prepare("UPDATE jobs SET enabled = 0 WHERE id = ?1 AND account_id = ?2").bind(id, account.id).run();
      return r.meta.changes ? json({ enabled: b.enabled }) : err(404, "That cron doesn't exist.");
    }
  }
  return err(404, "Not found.");
}

async function handleAuth(request, env, url) {
  if (url.pathname !== "/auth/callback") return err(404, "Not found.");
  const back = (q) => new Response(null, { status: 302, headers: { Location: `/${q}`, "Set-Cookie": cookie("fc_state", "", { path: "/auth" }), ...SEC } });
  const code = url.searchParams.get("code"), state = url.searchParams.get("state") || "";
  const want = L.parseCookies(request.headers.get("Cookie")).fc_state || "";
  if (!code || !state || !want || !L.safeEqual(state, want) || !/^[A-Za-z0-9_-]{6,200}$/.test(code)) return back("?error=state");
  try {
    const t = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", "User-Agent": UA },
      body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code, redirect_uri: `${url.origin}/auth/callback` }),
    }).then((r) => r.json());
    if (!t.access_token) return back("?error=github");
    const u = await fetch("https://api.github.com/user", { headers: { Authorization: `Bearer ${t.access_token}`, "User-Agent": UA, Accept: "application/vnd.github+json" } }).then((r) => r.json());
    if (!Number.isInteger(u.id) || typeof u.login !== "string") return back("?error=github");
    // the GitHub token is used once for the identity and then dropped: it is never stored or logged
    const now = Date.now();
    await env.DB.prepare(
      `INSERT INTO accounts (github_id, login, created_at) VALUES (?1, ?2, ?3)
       ON CONFLICT(github_id) DO UPDATE SET login = excluded.login`).bind(u.id, u.login.slice(0, 39), now).run();
    const acct = await env.DB.prepare("SELECT id FROM accounts WHERE github_id = ?1").bind(u.id).first();
    const sess = await L.signToken({ id: acct.id, exp: now + L.SESSION_DAYS * 86400000 }, env.SESSION_SECRET);
    const res = back("");
    res.headers.append("Set-Cookie", cookie("fc_session", sess, { maxAge: L.SESSION_DAYS * 86400 }));
    return res;
  } catch { return back("?error=github"); }
}

export async function runDue(env, now = Date.now(), doFetch = fetch) {
  if (String(env.DISABLED || "") === "1") return { skipped: "disabled" };
  await ensureRunsTable(env); await ensureAlertTables(env);
  const blocked = selfHosts(env, null);
  const claimed = ((await env.DB.prepare(
    `UPDATE jobs SET next_run_at = ?1 + 3600000, last_run_at = ?1
     WHERE id IN (
       SELECT j.id FROM jobs j JOIN accounts a ON a.id = j.account_id
       WHERE j.enabled = 1 AND j.next_run_at <= ?1
         AND (a.premium_until > ?1 OR (SELECT COUNT(*) FROM jobs k WHERE k.account_id = j.account_id AND k.id <= j.id) <= ?2)
       ORDER BY j.next_run_at LIMIT ?3)
     RETURNING *`).bind(now, L.FREE_LIMIT, L.BATCH).all()).results) || [];
  const out = { ran: 0, ok: 0, failed: 0 };
  await Promise.all(claimed.map(async (job) => {
    const acct = await env.DB.prepare("SELECT a.*, (s.discord_webhook IS NOT NULL AND s.alerts_enabled = 1) AS can_alert FROM accounts a LEFT JOIN account_settings s ON s.account_id = a.id WHERE a.id = ?1").bind(job.account_id).first();
    // advance BEFORE running, so a slow or crashed run can't be picked up again by the next tick
    await env.DB.prepare("UPDATE jobs SET next_run_at = ?2 WHERE id = ?1").bind(job.id, L.nextRunAt(now, job.every_minutes, acct)).run();
    let status = 0, ms = 0;
    const started = Date.now();
    try {
      if (!L.validateUrl(job.url, blocked).url) throw new Error("blocked");      // re-checked on every run
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), L.TIMEOUT_MS);
      const init = { method: job.method, redirect: "manual", signal: ctl.signal, headers: { "User-Agent": UA, "X-FreeCron-Job": String(job.id) } };
      if (job.method === "POST" && job.body) { init.body = job.body; init.headers["Content-Type"] = "application/json"; }
      const r = await doFetch(job.url, init);
      clearTimeout(timer);
      status = r.status;
      try { await r.body?.cancel(); } catch { /* the response body is never read or stored */ }
    } catch { status = 0; }
    ms = Date.now() - started;
    const ok = L.classify(status);
    out.ran++; ok ? out.ok++ : out.failed++;
    const failCount = ok ? 0 : job.fail_count + 1;
    const al = L.alertDecision({ prevAlerting: !!job.alerting, ok, failCount, canAlert: !!acct?.can_alert });
    const stmts = [                                            // one D1 call: the job's new state, its run row and any alert together
      env.DB.prepare(
        `UPDATE jobs SET last_status = ?2, last_ms = ?3,
           fail_count = CASE WHEN ?4 THEN 0 ELSE fail_count + 1 END,
           enabled = CASE WHEN ?4 THEN enabled WHEN fail_count + 1 >= ?5 THEN 0 ELSE enabled END,
           alerting = ?6, last_alert_at = CASE WHEN ?7 THEN ?8 ELSE last_alert_at END
         WHERE id = ?1`).bind(job.id, status, ms, ok ? 1 : 0, L.MAX_FAILS, al.alerting, al.kind ? 1 : 0, now),
      env.DB.prepare("INSERT INTO runs (job_id, account_id, ran_at, status, ms, ok) VALUES (?1, ?2, ?3, ?4, ?5, ?6)").bind(job.id, job.account_id, started, status, ms, ok ? 1 : 0),
    ];
    if (al.kind) {                                             // queue it (at most ALERTS_PER_DAY per account per 24 h), then count it; the order matters
      stmts.push(
        env.DB.prepare(`INSERT INTO alert_outbox (account_id, job_id, kind, job_name, detail, created_at)
                        SELECT ?1, ?2, ?3, ?4, ?5, ?6 FROM account_settings WHERE account_id = ?1 AND (alert_window_start < ?7 OR alert_count < ?8)`)
          .bind(job.account_id, job.id, al.kind, job.name, L.failDetail(status), now, now - 86400000, L.ALERTS_PER_DAY),
        env.DB.prepare(`UPDATE account_settings SET alert_count = CASE WHEN alert_window_start < ?2 THEN 1 ELSE alert_count + 1 END,
                          alert_window_start = CASE WHEN alert_window_start < ?2 THEN ?3 ELSE alert_window_start END
                        WHERE account_id = ?1 AND (alert_window_start < ?2 OR alert_count < ?4)`).bind(job.account_id, now - 86400000, now, L.ALERTS_PER_DAY));
    }
    await env.DB.batch(stmts);
  }));
  return out;
}

/** Daily re-check of membership keys: still paying -> Premium rolls forward; cancelled / refunded / failed -> it simply lapses. */
export async function recheckLicenses(env, now = Date.now(), doFetch = fetch) {
  if (String(env.DISABLED || "") === "1" || !env.GUMROAD_PRODUCT_ID) return { skipped: true };
  await ensureLicenseTable(env);
  const rows = (await env.DB.prepare("SELECT * FROM licenses WHERE active = 1 AND recurring = 1 AND checked_at < ?1 ORDER BY checked_at LIMIT ?2").bind(now - L.RECHECK_MS, L.RECHECK_BATCH).all()).results || [];
  const out = { checked: 0, renewed: 0, ended: 0, retry: 0 };
  for (const r of rows) {
    const g = await gumroadVerify(env, r.license_key, doFetch);
    out.checked++;
    if (g.net) { out.retry++; await env.DB.prepare("UPDATE licenses SET checked_at = ?2 WHERE key_hash = ?1").bind(r.key_hash, now - L.RECHECK_MS + 3600000).run(); continue; }   // try again in about an hour
    if (L.judgeLicense(g.resp, env.GUMROAD_PRODUCT_ID).ok) {
      out.renewed++;
      await env.DB.batch([
        env.DB.prepare("UPDATE licenses SET checked_at = ?2 WHERE key_hash = ?1").bind(r.key_hash, now),
        env.DB.prepare("UPDATE accounts SET premium_until = MAX(premium_until, ?2) WHERE id = ?1").bind(r.account_id, now + L.GRACE_DAYS * 86400000),
      ]);
    } else {
      out.ended++;
      await env.DB.prepare("UPDATE licenses SET active = 0, checked_at = ?2 WHERE key_hash = ?1").bind(r.key_hash, now).run();
    }
  }
  return out;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" && request.method === "GET") {
      const nonce = L.randomId(16);
      return new Response(pageHtml(nonce, env.TURNSTILE_SITE_KEY || ""), { headers: {
        "Content-Type": "text/html; charset=utf-8", ...SEC,
        "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}' https://challenges.cloudflare.com; style-src 'nonce-${nonce}'; frame-src https://challenges.cloudflare.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
      } });
    }
    const sm = url.pathname.match(/^\/s\/([a-z0-9-]{3,40})$/);
    if (sm && request.method === "GET") {                                  // public status page: its own strict CSP, no script at all
      const nonce = L.randomId(16), data = await publicStatus(env, sm[1]);
      return new Response(data ? statusHtml(data, nonce, env.CONTACT_URL || "") : notFoundHtml(nonce), { status: data ? 200 : 404, headers: {
        "Content-Type": "text/html; charset=utf-8", ...SEC, "Cache-Control": data ? "public, max-age=60" : "no-store", "X-Robots-Tag": "noindex, nofollow",
        "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
      } });
    }
    if ((url.pathname === "/privacy" || url.pathname === "/terms") && request.method === "GET") {
      const nonce = L.randomId(16);
      return new Response(legalPage(url.pathname.slice(1), nonce, env.CONTACT_URL || ""), { headers: {
        "Content-Type": "text/html; charset=utf-8", ...SEC, "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
      } });
    }
    if (url.pathname.startsWith("/api/")) return handleApi(request, env, url);
    if (url.pathname.startsWith("/auth/")) return handleAuth(request, env, url);
    return err(404, "Not found.");
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => { try { await runDue(env); } finally { await sendAlerts(env).catch(() => null); } })());   // alerts go out right after the crons ran
    ctx.waitUntil(recheckLicenses(env).catch(() => null));
    if (Math.floor(Date.now() / 300000) % 12 === 0) { ctx.waitUntil(pruneRuns(env).catch(() => null)); ctx.waitUntil(pruneOutbox(env).catch(() => null)); }   // about once an hour, saves subrequests
  },
};
