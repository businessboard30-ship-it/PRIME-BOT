import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../src/logic.js";

const NOW = 1_800_000_000_000;
const free = { id: 1, premium_until: 0 };
const prem = { id: 2, premium_until: NOW + 86400000 };

test("urls: only public https hostnames", () => {
  for (const ok of ["https://example.com/ping", "https://api.example.co.uk/a?b=1", "https://xn--bcher-kva.example.org/x", "https://EXAMPLE.com:443/x"])
    assert.ok(L.validateUrl(ok).url, ok);
  for (const bad of ["http://example.com", "https://127.0.0.1/", "https://10.0.0.5/", "https://192.168.1.1/", "https://169.254.169.254/latest/meta-data",
    "https://[::1]/", "https://[fd00::1]/", "https://2130706433/", "https://0x7f.0.0.1/", "https://0177.0.0.1/", "https://localhost/", "https://foo.localhost/",
    "https://printer.local/", "https://db.internal/", "https://user:pw@example.com/", "https://example.com:8443/", "https://example/", "ftp://example.com", "javascript:alert(1)",
    "", "   ", "https://", "https://exa mple.com", "https://-bad-.com", "https://example.123", "https://metadata.google.internal/", "x".repeat(3000)])
    assert.ok(L.validateUrl(bad).error, bad);
});
test("urls: this service's own host is refused (no loops)", () => {
  assert.ok(L.validateUrl("https://free-cron.example.workers.dev/api", ["free-cron.example.workers.dev"]).error);
  assert.ok(L.validateUrl("https://a.free-cron.example.workers.dev/", ["free-cron.example.workers.dev"]).error);
  assert.ok(L.validateUrl("https://other.workers.dev/", ["free-cron.example.workers.dev"]).url);
});
test("urls: fragment dropped, trailing dot ok", () => {
  assert.equal(L.validateUrl("https://example.com./x#frag").url, "https://example.com./x");
});
const good = (o = {}) => ({ name: "Ping", url: "https://example.com/p", method: "GET", every_minutes: 15, ...o });
test("jobs: free floor is 15 minutes, premium 5", () => {
  assert.ok(L.validateJob(good(), free, NOW).spec);
  assert.ok(L.validateJob(good({ every_minutes: 5 }), free, NOW).error);
  assert.ok(L.validateJob(good({ every_minutes: 5 }), prem, NOW).spec);
  assert.ok(L.validateJob(good({ every_minutes: 1 }), prem, NOW).error);
  for (const bad of [0, -5, 7, 1.5, "x", null, 1441, 10080]) assert.ok(L.validateJob(good({ every_minutes: bad }), prem, NOW).error, String(bad));
});
test("jobs: lapsed premium is free again", () => {
  assert.ok(L.validateJob(good({ every_minutes: 5 }), { premium_until: NOW - 1 }, NOW).error);
  assert.equal(L.limitFor({ premium_until: NOW - 1 }, NOW), 5);
  assert.equal(L.limitFor(prem, NOW), Infinity);
});
test("jobs: validation of name, method, body", () => {
  assert.ok(L.validateJob(good({ name: "  " }), free, NOW).error);
  assert.equal(L.validateJob(good({ name: "a".repeat(200) }), free, NOW).spec.name.length, L.MAX_NAME);
  assert.ok(L.validateJob(good({ method: "DELETE" }), free, NOW).error);
  assert.equal(L.validateJob(good({ method: "post", body: '{"a":1}' }), free, NOW).spec.body, '{"a":1}');
  assert.equal(L.validateJob(good({ method: "GET", body: "ignored" }), free, NOW).spec.body, null);
  assert.ok(L.validateJob(good({ method: "POST", body: "x".repeat(1025) }), free, NOW).error);
  assert.ok(L.validateJob(good({ method: "POST", body: { a: 1 } }), free, NOW).error);
  assert.ok(L.validateJob(null, free, NOW).error);
});
test("schedule: never closer than the plan floor", () => {
  assert.equal(L.nextRunAt(NOW, 60, free), NOW + 3600000);
  assert.equal(L.nextRunAt(NOW, 5, free), NOW + 15 * 60000);   // premium lapsed: clamped up
  assert.equal(L.nextRunAt(NOW, 5, prem), NOW + 5 * 60000);
});
test("classify", () => {
  for (const s of [200, 204, 301, 399]) assert.ok(L.classify(s));
  for (const s of [199, 400, 404, 500, 0, NaN, undefined, null]) assert.ok(!L.classify(s));
});
test("session tokens: sign, verify, tamper, expiry, wrong secret", async () => {
  const t = await L.signToken({ id: 7, login: "a", exp: NOW + 1000 }, "s3cret-s3cret");
  assert.equal((await L.verifyToken(t, "s3cret-s3cret", NOW)).id, 7);
  assert.equal(await L.verifyToken(t, "other-secret", NOW), null);
  assert.equal(await L.verifyToken(t, "s3cret-s3cret", NOW + 2000), null);
  const [b, s] = t.split(".");
  const forged = btoa(JSON.stringify({ id: 1, login: "admin", exp: NOW + 1000 })).replace(/=+$/, "");
  assert.equal(await L.verifyToken(`${forged}.${s}`, "s3cret-s3cret", NOW), null);
  for (const bad of ["", "x", "a.b", "a.b.c", null, undefined, 5, "x".repeat(3000)]) assert.equal(await L.verifyToken(bad, "s3cret-s3cret", NOW), null);
  assert.equal(await L.verifyToken(t, "", NOW), null);
});
test("cookies, ids, safeEqual", () => {
  assert.deepEqual(L.parseCookies("a=1; b=two; junk; c=x=y"), { a: "1", b: "two", c: "x=y" });
  assert.notEqual(L.randomId(), L.randomId());
  assert.ok(L.randomId().length >= 30);
  assert.ok(L.safeEqual("abc", "abc") && !L.safeEqual("abc", "abd") && !L.safeEqual("abc", "abcd") && !L.safeEqual("", "x"));
});
test("csrf check", () => {
  const url = new URL("https://x.dev/api/jobs");
  const req = (h) => ({ headers: { get: (k) => h[k] ?? null } });
  assert.ok(L.sameOriginOk(req({ "X-FreeCron": "1" }), url));
  assert.ok(L.sameOriginOk(req({ "X-FreeCron": "1", Origin: "https://x.dev" }), url));
  assert.ok(!L.sameOriginOk(req({ "X-FreeCron": "1", Origin: "https://evil.dev" }), url));
  assert.ok(!L.sameOriginOk(req({}), url));
});

test("tally: enabled, disabled, ok and failed for the dashboard tiles", () => {
  assert.deepEqual(L.tally([]), { enabled: 0, disabled: 0, ok: 0, failed: 0 });
  const jobs = [
    { enabled: true, over_limit: false, last_run_at: 1, last_status: 200 },   // ok
    { enabled: true, over_limit: false, last_run_at: 1, last_status: 301 },   // 3xx counts as ok
    { enabled: true, over_limit: false, last_run_at: 1, last_status: 500 },   // failed
    { enabled: true, over_limit: false, last_run_at: 1, last_status: 0 },     // timeout = failed
    { enabled: false, over_limit: false, last_run_at: 0, last_status: null }, // paused, never ran
    { enabled: true, over_limit: true, last_run_at: 0, last_status: null },   // over the free limit = disabled
    { enabled: true, over_limit: false, last_run_at: 0, last_status: null },  // new, never ran: neither ok nor failed
  ];
  assert.deepEqual(L.tally(jobs), { enabled: 5, disabled: 2, ok: 2, failed: 2 });
});

test("runsLimit: default 50, minimum 1, maximum 100", () => {
  assert.equal(L.runsLimit(null), 50); assert.equal(L.runsLimit(undefined), 50); assert.equal(L.runsLimit("abc"), 50);
  assert.equal(L.runsLimit("0"), 50); assert.equal(L.runsLimit("-5"), 50);
  assert.equal(L.runsLimit("1"), 1); assert.equal(L.runsLimit("100"), 100); assert.equal(L.runsLimit("101"), 100); assert.equal(L.runsLimit("99999"), 100);
});
