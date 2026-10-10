// Pure rules for the free cron service. No network, no D1, no secrets: everything here is unit-tested (test/logic.test.js).

export const FREE_LIMIT = 5;                 // crons per free account
export const FREE_MIN_MINUTES = 15;
export const PREMIUM_MIN_MINUTES = 5;        // the trigger ticks every 5 minutes, so this is the floor for everyone
export const INTERVALS = [5, 15, 30, 60, 360, 720, 1440];   // allowlist; there is no free-form cron string
export const MAX_NAME = 60;
export const MAX_URL = 2000;
export const MAX_BODY = 1024;
export const MAX_FAILS = 5;
export const TIMEOUT_MS = 10000;
export const BATCH = 40;                     // jobs per tick (Workers cap subrequests per invocation)
export const CREATES_PER_DAY = 20;
export const SESSION_DAYS = 7;

const BAD_SUFFIXES = [".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".private", ".arpa", ".onion", ".test", ".invalid", ".example"];
const TLD = /^([a-z]{2,63}|xn--[a-z0-9-]{1,59})$/;
const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

export function isPremium(account, now) {
  return !!account && Number(account.premium_until || 0) > now;
}
export function limitFor(account, now) { return isPremium(account, now) ? Infinity : FREE_LIMIT; }
export function minMinutesFor(account, now) { return isPremium(account, now) ? PREMIUM_MIN_MINUTES : FREE_MIN_MINUTES; }

/** Only public https URLs on the default port, by hostname. Returns {url} or {error}. `blocked` = extra hostnames to refuse (this service itself). */
export function validateUrl(raw, blocked = []) {
  if (typeof raw !== "string" || !raw.trim() || raw.length > MAX_URL) return { error: "Enter a full https:// address." };
  let u;
  try { u = new URL(raw.trim()); } catch { return { error: "That isn't a valid address." }; }
  if (u.protocol !== "https:") return { error: "Only https:// addresses are allowed." };
  if (u.username || u.password) return { error: "Addresses with a username or password aren't allowed." };
  if (u.port && u.port !== "443") return { error: "Only the standard https port is allowed." };
  const host = u.hostname.toLowerCase().replace(/\.$/, "");
  if (!host || host.length > 253) return { error: "That isn't a valid address." };
  if (host.includes(":") || host.startsWith("[")) return { error: "IP addresses aren't allowed. Use a domain name." };
  const labels = host.split(".");
  if (labels.length < 2 || !labels.every((l) => LABEL.test(l))) return { error: "That isn't a valid public domain." };
  if (/^[0-9.]+$/.test(host) || /^(0x[0-9a-f]+|[0-9]+)(\.|$)/.test(host) && labels.every((l) => /^(0x[0-9a-f]+|[0-9]+)$/.test(l))) return { error: "IP addresses aren't allowed. Use a domain name." };
  if (!TLD.test(labels[labels.length - 1])) return { error: "That isn't a valid public domain." };
  if (host === "localhost" || BAD_SUFFIXES.some((s) => host.endsWith(s))) return { error: "That address isn't public." };
  for (const b of blocked) { const x = String(b || "").toLowerCase().trim(); if (x && (host === x || host.endsWith("." + x))) return { error: "That address isn't allowed." }; }
  u.hash = "";
  return { url: u.toString() };
}

/** Validate a create request. Returns {spec} or {error}. */
export function validateJob(body, account, now, blocked = []) {
  const b = body && typeof body === "object" ? body : {};
  const name = String(b.name || "").replace(/\s+/g, " ").trim().slice(0, MAX_NAME);
  if (!name) return { error: "Give the cron a name." };
  const u = validateUrl(b.url, blocked);
  if (u.error) return u;
  const method = String(b.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "POST") return { error: "Method must be GET or POST." };
  let reqBody = null;
  if (method === "POST" && b.body != null && b.body !== "") {
    if (typeof b.body !== "string" || new TextEncoder().encode(b.body).length > MAX_BODY) return { error: `The body can be at most ${MAX_BODY} bytes.` };
    reqBody = b.body;
  }
  const every = Number(b.every_minutes);
  if (!Number.isInteger(every) || !INTERVALS.includes(every)) return { error: "Pick one of the listed intervals." };
  if (every < minMinutesFor(account, now)) return { error: `Free crons run at most every ${FREE_MIN_MINUTES} minutes. Premium allows every ${PREMIUM_MIN_MINUTES}.` };
  return { spec: { name, url: u.url, method, body: reqBody, every_minutes: every } };
}

/** Next slot: now + interval, never closer than the plan's floor (a lapsed premium account is clamped up to 15 minutes). */
export function nextRunAt(now, everyMinutes, account) {
  return now + Math.max(everyMinutes, minMinutesFor(account, now)) * 60000;
}

export function classify(status) {      // redirects are never followed; a 2xx/3xx answer counts as success
  return Number.isInteger(status) && status >= 200 && status < 400;
}

// Dashboard tiles. enabled = switched on and inside the plan limit; ok/failed = based on each cron's last run (never-run crons count as neither).
export function tally(jobs) {
  const t = { enabled: 0, disabled: 0, ok: 0, failed: 0 };
  for (const j of jobs) {
    if (j.enabled && !j.over_limit) t.enabled++; else t.disabled++;
    if (j.last_run_at) { if (classify(j.last_status)) t.ok++; else t.failed++; }
  }
  return t;
}

// ---- signed session cookie (HMAC-SHA256, base64url) ----
const enc = new TextEncoder();
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
async function hmac(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}
export async function signToken(payload, secret) {
  const body = b64u(enc.encode(JSON.stringify(payload)));
  const sig = b64u(await crypto.subtle.sign("HMAC", await hmac(secret), enc.encode(body)));
  return `${body}.${sig}`;
}
export async function verifyToken(token, secret, now) {
  try {
    if (typeof token !== "string" || !secret || token.length > 2000) return null;
    const [body, sig] = token.split(".");
    if (!body || !sig) return null;
    if (!(await crypto.subtle.verify("HMAC", await hmac(secret), unb64u(sig), enc.encode(body)))) return null;
    const p = JSON.parse(new TextDecoder().decode(unb64u(body)));
    return p && typeof p.exp === "number" && p.exp > now ? p : null;
  } catch { return null; }
}

export function parseCookies(header) {
  const out = {};
  for (const part of String(header || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

export function randomId(bytes = 24) {
  return b64u(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** Constant-time string compare for the admin key. */
export function safeEqual(a, b) {
  const x = enc.encode(String(a)), y = enc.encode(String(b));
  let d = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) d |= (x[i] || 0) ^ (y[i] || 0);
  return d === 0;
}

/** CSRF: state-changing calls must carry our header and, when the browser sends an Origin, it must be ours. */
export function sameOriginOk(request, url) {
  if (request.headers.get("X-FreeCron") !== "1") return false;
  const o = request.headers.get("Origin");
  return !o || o === url.origin;
}

// ---- Gumroad Premium (license keys) ----
export const GRACE_DAYS = 3;                 // a membership stays Premium this long after its last good daily check
export const RECHECK_MS = 86400000;          // re-verify each membership key about once a day
export const RECHECK_BATCH = 5;              // keys re-verified per 5-minute tick (keeps subrequests low)

export function validKey(k) { return typeof k === "string" && /^[A-Za-z0-9-]{8,80}$/.test(k.trim()); }
export function daysFor(env) { const n = Number(env?.PREMIUM_DAYS); return Number.isInteger(n) && n >= 1 && n <= 3650 ? n : 30; }
export function isRecurring(env) { return String(env?.GUMROAD_RECURRING ?? "1") !== "0"; }
export function safeBuyUrl(u) {
  try { const x = new URL(String(u || "")); return x.protocol === "https:" && !x.username && !x.password ? x.toString() : null; } catch { return null; }
}
export async function sha256Hex(s) {
  const d = await crypto.subtle.digest("SHA-256", enc.encode(String(s)));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Judge Gumroad's /licenses/verify answer. Returns {ok:true} or {ok:false, error}. Only a definite "no" from Gumroad is a "no". */
export function judgeLicense(resp, productId) {
  const bad = (error) => ({ ok: false, error });
  if (!resp || resp.success !== true) return bad("That license key isn't valid for this product.");
  const p = resp.purchase && typeof resp.purchase === "object" ? resp.purchase : {};
  if (productId && p.product_id && p.product_id !== productId) return bad("That license key isn't valid for this product.");
  if (p.refunded || p.chargebacked || (p.disputed && !p.dispute_won)) return bad("That purchase was refunded or disputed.");
  if (p.subscription_ended_at || p.subscription_cancelled_at || p.subscription_failed_at) return bad("That subscription is no longer active.");
  return { ok: true };
}
