// free-cron: a standalone free cron service. GitHub sign-in, Turnstile on sign-in and create, 5 free crons, Premium unlimited.
// One Cloudflare cron trigger (every 5 minutes) runs every user's due jobs; D1 holds accounts and jobs. No bot, no Discord.
import * as L from "./logic.js";
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

  const account = await sessionOf(request, env);
  if (!account) return err(401, "Please sign in.");

  if (path === "/api/me" && method === "GET") return json(await me(env, account));
  if (path === "/api/me" && method === "DELETE") {                       // delete my account and every cron
    await ensureLicenseTable(env);
    await env.DB.batch([env.DB.prepare("DELETE FROM jobs WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM licenses WHERE account_id = ?1").bind(account.id), env.DB.prepare("DELETE FROM accounts WHERE id = ?1").bind(account.id)]);
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
    // per-account creation budget (20 / 24 h), counted atomically so deleting and re-creating can't dodge it
    const budget = await env.DB.prepare(
      `UPDATE accounts SET create_count = CASE WHEN create_window_start < ?2 THEN 1 ELSE create_count + 1 END,
                           create_window_start = CASE WHEN create_window_start < ?2 THEN ?3 ELSE create_window_start END
       WHERE id = ?1 AND (create_window_start < ?2 OR create_count < ?4)`).bind(account.id, now - 86400000, now, L.CREATES_PER_DAY).run();
    if (!budget.meta.changes) return err(429, "You've created a lot of crons today. Try again tomorrow.");
    const s = v.spec, lim = L.limitFor(account, now);
    const ins = await env.DB.prepare(
      `INSERT INTO jobs (account_id, name, url, method, body, every_minutes, next_run_at, created_at)
       SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8 WHERE (SELECT COUNT(*) FROM jobs WHERE account_id = ?1) < ?9`)
      .bind(account.id, s.name, s.url, s.method, s.body, s.every_minutes, L.nextRunAt(now, s.every_minutes, account), now, lim === Infinity ? 1e9 : lim).run();
    if (!ins.meta.changes) return err(422, `You can have ${L.FREE_LIMIT} free crons. Delete one or upgrade to Premium.`);
    return json({ id: ins.meta.last_row_id });
  }

  const m = path.match(/^\/api\/jobs\/(\d{1,12})$/);
  if (m) {
    const id = Number(m[1]);
    if (method === "DELETE") {
      const r = await env.DB.prepare("DELETE FROM jobs WHERE id = ?1 AND account_id = ?2").bind(id, account.id).run();
      return r.meta.changes ? json({ deleted: true }) : err(404, "That cron doesn't exist.");
    }
    if (method === "PATCH") {
      const b = await request.json().catch(() => ({}));
      if (typeof b.enabled !== "boolean") return err(422, "enabled must be true or false.");
      const now = Date.now();
      const r = b.enabled
        ? await env.DB.prepare("UPDATE jobs SET enabled = 1, fail_count = 0, next_run_at = ?3 WHERE id = ?1 AND account_id = ?2").bind(id, account.id, now + 60000).run()
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
    const acct = await env.DB.prepare("SELECT * FROM accounts WHERE id = ?1").bind(job.account_id).first();
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
    await env.DB.prepare(
      `UPDATE jobs SET last_status = ?2, last_ms = ?3,
         fail_count = CASE WHEN ?4 THEN 0 ELSE fail_count + 1 END,
         enabled = CASE WHEN ?4 THEN enabled WHEN fail_count + 1 >= ?5 THEN 0 ELSE enabled END
       WHERE id = ?1`).bind(job.id, status, ms, ok ? 1 : 0, L.MAX_FAILS).run();
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
    if (url.pathname.startsWith("/api/")) return handleApi(request, env, url);
    if (url.pathname.startsWith("/auth/")) return handleAuth(request, env, url);
    return err(404, "Not found.");
  },
  async scheduled(event, env, ctx) { ctx.waitUntil(runDue(env)); ctx.waitUntil(recheckLicenses(env).catch(() => null)); },
};
