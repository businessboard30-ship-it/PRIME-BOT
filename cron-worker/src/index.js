// Cloudflare Worker replacing .github/workflows/cron.yml.
// Cron Triggers fire on time (GitHub schedules run minutes late / get skipped).
// Needs Worker secret CRON_SECRET (same value as on the backend).
// BACKEND_URL is a plain var in wrangler.toml (same as redirector/config.json backend_url).

const GROUPS = {
  frequent: ["cron_discord_announcements", "cron_discord_owner_broadcast", "cron_dash_dropbox_dm", "cron_dev_scheduled"],
  hourly: ["cron_ad_placement"],
  daily: ["cron_expire_monetization", "cron_renew_yandex_search", "cron_cleanup_pending_payments"],
};

// cron expression (must match wrangler.toml) -> group
const SCHEDULES = {
  "*/5 * * * *": "frequent",
  "17 * * * *": "hourly",
  "23 3 * * *": "daily",
};

async function callEndpoint(env, ep) {
  const base = String(env.BACKEND_URL || "").replace(/\/+$/, "");
  const url = `${base}/api/${ep}`;
  const started = Date.now();
  // 1 try + 2 retries, 5s apart (mirrors curl --retry 2 --retry-delay 5)
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.CRON_SECRET}` },
        signal: AbortSignal.timeout(90000),
      });
      const ms = Date.now() - started;
      if (res.ok) {
        console.log(`${ep} -> HTTP ${res.status} (${ms}ms)`);
        return true;
      }
      const body = (await res.text()).slice(0, 300);
      console.error(`${ep} -> HTTP ${res.status} (attempt ${attempt + 1}) ${body}`);
      if (res.status < 500 && res.status !== 429 && res.status !== 408) return false; // don't retry 4xx
    } catch (err) {
      console.error(`${ep} -> ERROR ${err.message} (attempt ${attempt + 1})`);
    }
    if (attempt < 2) await new Promise((r) => setTimeout(r, 5000));
  }
  return false;
}

async function runGroup(env, group) {
  if (!env.CRON_SECRET) {
    console.error("CRON_SECRET is not set on this Worker");
    return { group, ok: false, results: {} };
  }
  const eps = GROUPS[group] || [];
  const results = {};
  await Promise.all(eps.map(async (ep) => { results[ep] = await callEndpoint(env, ep); }));
  return { group, ok: Object.values(results).every(Boolean), results };
}

export default {
  async scheduled(event, env, ctx) {
    const group = SCHEDULES[event.cron];
    if (!group) {
      console.error(`Unknown cron expression: ${event.cron}`);
      return;
    }
    ctx.waitUntil(runGroup(env, group));
  },

  // Manual trigger: GET /?group=frequent|hourly|daily  with header Authorization: Bearer <CRON_SECRET>
  async fetch(request, env) {
    const auth = request.headers.get("Authorization") || "";
    if (!env.CRON_SECRET || auth !== `Bearer ${env.CRON_SECRET}`) {
      return new Response("cron-pinger: ok", { status: 200 });
    }
    const group = new URL(request.url).searchParams.get("group") || "frequent";
    if (!GROUPS[group]) return new Response("unknown group", { status: 400 });
    const out = await runGroup(env, group);
    return Response.json(out, { status: out.ok ? 200 : 502 });
  },
};
