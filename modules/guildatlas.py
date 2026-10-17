"""GuildAtlas integration: signed calls to the site, site-health monitoring, and the AI knowledge cache.

Pure logic lives here (testable without Discord). The Discord side is discord_bot/cogs/guildatlas.py.
The wire contract is documented in the GuildAtlas repo: docs/bot-integration.md.

Environment (all optional; with the secret or URL missing the integration stays off and says so in the logs):
    GUILDATLAS_SITE_URL            e.g. https://guildatlas.example   (no trailing slash needed)
    GUILDATLAS_INTERNAL_SECRET     must equal INTERNAL_API_SECRET on the site (32+ random chars)
    GUILDATLAS_ALERT_CHANNEL_ID    optional private channel for "site is down" alerts from this bot
Never log the secret, signatures or request bodies with tokens in them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("guildatlas")

REVIEW_CUSTOM_ID = re.compile(r"^ga:r:(approve|request_changes|reject):(\d{1,12})$")
NEEDS_REASON = {"request_changes", "reject"}
REASON_MIN, REASON_MAX = 5, 500
SNOWFLAKE = re.compile(r"^\d{17,20}$")
INVITE_CODE = re.compile(r"^[A-Za-z0-9-]{2,32}$")
DISGUILD_NAME = "Disguild"
PRESENCE_CHUNK = 300  # the site accepts at most 300 ids per call

# Payments (Disguild Phase 33): this bot owns the Gumroad account's single Ping URL, so pings for Disguild orders are forwarded to the site.
DISGUILD_REF_PREFIX = "ga_"
PING_FIELDS = ("sale_id", "test", "refunded", "disputed", "dispute_won", "chargebacked", "resource_name",
               "url_params[reference]", "reference", "permalink", "product_permalink", "price")
MAX_SERVERS, MAX_SHARDS = 10_000_000, 10_000  # the site rejects anything outside these
MAX_MEMBERS = 100_000_000  # the site rejects anything above this
GUILD_STATS_CHUNK = 100  # entries per call (the site accepts at most 100)
INVITE_REASON = "Permanent invite for the Disguild listing (requested by a server manager)"

# Web log (Disguild Phase 38): the site records activity in web_events; this bot posts it to a private channel in the support server.
WEBLOG_CHANNEL_NAME = "web-logs"
WEBLOG_CURSOR_KEY = "disguild_weblog_cursor"
WEBLOG_CHANNEL_KEY = "disguild_weblog_channel"
WEBLOG_EMBEDS_PER_MESSAGE = 10
_COLORS = {"good": 0x57A773, "bad": 0xC0504D, "warn": 0xD9A441, "info": 0x7289DA}
_RATING = {1: "1/5", 2: "2/5", 3: "3/5", 4: "4/5", 5: "5/5"}


def _clip(text, n: int) -> str:
    return str(text or "")[:n]


def format_web_event(event: dict, site_url: str = "") -> Optional[dict]:
    """One feed event -> {"title", "description", "color"} for an embed, or None for something this bot does not know.
    Counts and names only; the site never sends voter, reporter or review-author identity, and neither do we invent any."""
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    listing = data.get("listing") if isinstance(data.get("listing"), dict) else {}
    lt = "Server" if listing.get("type") == "server" else "Bot"
    name = _clip(listing.get("name"), 80) or "unknown listing"
    owner = _clip(listing.get("owner"), 40)
    slug = listing.get("slug") if isinstance(listing.get("slug"), str) and re.match(r"^[a-z0-9-]{1,80}$", listing.get("slug", "")) else None
    path = f"{'servers' if lt == 'Server' else 'bots'}/{slug}" if slug else None
    link = f"\n{site_url.rstrip('/')}/{path}" if site_url and path else ""
    who = f" by {owner}" if owner else ""
    q = data.get("queue") if isinstance(data.get("queue"), dict) else {}
    pos, waiting = q.get("position"), q.get("waiting")
    queue_line = f"\nQueue number **#{pos}** ({waiting} waiting for review)" if isinstance(pos, int) and isinstance(waiting, int) else ""

    if kind == "listing.submitted":
        return {"title": f"{lt} added", "description": f"**{name}**{who} was added and is waiting for review.{queue_line}{link}", "color": _COLORS["info"]}
    if kind == "listing.resubmitted":
        return {"title": f"{lt} resubmitted", "description": f"**{name}**{who} was sent back for review.{queue_line}{link}", "color": _COLORS["info"]}
    simple = {
        "listing.approved": (f"{lt} approved", "good"), "listing.rejected": (f"{lt} rejected", "bad"),
        "listing.changes_requested": (f"Changes requested on a {lt.lower()}", "warn"), "listing.suspended": (f"{lt} suspended", "bad"),
        "listing.restored": (f"{lt} restored", "good"), "edit.submitted": (f"Edit sent for review ({lt.lower()})", "info"),
        "edit.approved": (f"Edit approved ({lt.lower()})", "good"), "edit.rejected": (f"Edit rejected ({lt.lower()})", "bad"),
        "appeal.submitted": ("Appeal sent", "warn"), "appeal.upheld": ("Appeal accepted", "good"), "appeal.denied": ("Appeal denied", "bad"),
    }
    if kind in simple:
        title, color = simple[kind]
        extra = queue_line if kind == "appeal.upheld" else ""
        return {"title": title, "description": f"**{name}**{who}{extra}{link}", "color": _COLORS[color]}
    if kind == "vote":
        n = data.get("vote_count")
        return {"title": "New vote", "description": f"**{name}** now has **{n}** vote(s).{link}" if isinstance(n, int) else f"**{name}** got a vote.{link}", "color": _COLORS["info"]}
    if kind == "review":
        r = _RATING.get(data.get("rating"))
        return {"title": "New review", "description": f"**{name}** got a review{f' ({r})' if r else ''}.{link}", "color": _COLORS["info"]}
    if kind == "report":
        what = "review" if data.get("onReview") else lt.lower()
        return {"title": "New report", "description": f"A {what} on **{name}** was reported (reason: {_clip(data.get('reason'), 40) or 'not given'}). Open the site's admin reports.", "color": _COLORS["warn"]}
    if kind == "boost":
        d = data.get("days")
        return {"title": "Boost started", "description": f"**{name}** was boosted{f' for {d} days' if isinstance(d, int) else ''}.{link}", "color": _COLORS["good"]}
    return None


# Queue questions: only these get the asker's own queue numbers added to the AI prompt.
QUEUE_QUESTION = re.compile(r"\bqueue\b|\b(my|the)\s+(bot|server|listing|submission)\b.{0,40}\b(position|line|turn|status|waiting|pending|review)\b|\b(position|place)\s+in\s+(the\s+)?(line|queue)\b|\bhow\s+long\b.{0,40}\b(review|approv)", re.I | re.S)


def is_queue_question(text: str) -> bool:
    return bool(text) and bool(QUEUE_QUESTION.search(text))


def queue_context(data: dict, site_url: str = "") -> str:
    """Prompt text with the ASKER's own queue numbers (from GET /api/internal/queue). Statuses and places only."""
    if not isinstance(data, dict) or not data.get("ok"):
        return ""
    waiting = data.get("waiting") if isinstance(data.get("waiting"), int) else 0
    lines = [f"Disguild review queue right now: {waiting} listing(s) waiting. Place 1 is reviewed next. "
             "Below are ONLY the listings of the person asking, as the site reports them. Quote these exactly; never guess a number."]
    items = data.get("listings") if isinstance(data.get("listings"), list) else []
    if not items:
        lines.append("This person has no listing waiting, needing changes, rejected or suspended. If they expect one, tell them to check their dashboard "
                     "(they may not have signed in with the same Discord account, or it is already approved).")
    for it in items[:10]:
        if not isinstance(it, dict):
            continue
        nm, st, pos = _clip(it.get("name"), 80), it.get("status"), it.get("position")
        if st == "pending" and isinstance(pos, int):
            lines.append(f"- {nm} ({it.get('type')}): waiting for review, queue number {pos} of {waiting}.")
        else:
            lines.append(f"- {nm} ({it.get('type')}): status {st} (not in the review line; the owner sees the reason on their dashboard).")
    if site_url:
        lines.append(f"Dashboard: {site_url.rstrip('/')}/dashboard")
    return "\n".join(lines)


def chunked(items, size: int = PRESENCE_CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def stats_entry(application_id, server_count, shard_count=None) -> Optional[dict]:
    """-> one entry for POST /api/internal/bot-stats, or None when a value is not something the site would accept.
    Counts only; a bot that is not sharded (shard_count None) sends no shard count."""
    app = str(application_id) if application_id is not None else ""
    if not SNOWFLAKE.match(app):
        return None
    if isinstance(server_count, bool) or not isinstance(server_count, int) or not 0 <= server_count <= MAX_SERVERS:
        return None
    entry = {"applicationId": app, "serverCount": server_count}
    if shard_count is not None:
        if isinstance(shard_count, bool) or not isinstance(shard_count, int) or not 1 <= shard_count <= MAX_SHARDS:
            return None
        entry["shardCount"] = shard_count
    return entry


def guild_stats_entries(guilds, listed_ids) -> list:
    """-> entries for POST /api/internal/guild-stats: one per LISTED server that this process is itself in (``guilds`` are the
    guild objects this bot process can see, so a clone only ever reports its own servers). Counts only. A guild whose member
    count is unknown or out of range is skipped rather than sent wrong."""
    wanted = {int(i) for i in listed_ids if isinstance(i, (int, str)) and SNOWFLAKE.match(str(i))}
    out = []
    for g in guilds:
        gid = getattr(g, "id", None)
        n = getattr(g, "member_count", None)
        if gid not in wanted or isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_MEMBERS:
            continue
        out.append({"guildId": str(gid), "memberCount": n})
    return out


def presence_delta(previous: set, current: set) -> tuple[list, list]:
    """-> (ids to report as present, ids that disappeared since the last report). Everything current is re-sent each time
    (the site treats a report as fresh for 3 hours), so a missed call heals itself."""
    return sorted(current), sorted(previous - current)


def invite_code_from(value) -> Optional[str]:
    code = str(value or "").strip()
    return code if INVITE_CODE.match(code) else None


# ── configuration ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    site_url: str
    secret: str
    alert_channel_id: int

    @property
    def enabled(self) -> bool:
        return bool(self.site_url and len(self.secret) >= 32)


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    raw_alert = (env.get("GUILDATLAS_ALERT_CHANNEL_ID") or "").strip()
    return Settings(
        site_url=(env.get("GUILDATLAS_SITE_URL") or "").strip().rstrip("/"),
        secret=(env.get("GUILDATLAS_INTERNAL_SECRET") or "").strip(),
        alert_channel_id=int(raw_alert) if SNOWFLAKE.match(raw_alert) else 0,
    )


# ── signing (must match GuildAtlas src/lib/server/internal-auth.ts) ─────────
def sign(secret: str, timestamp_ms: str, nonce: str, method: str, path_and_query: str, body: str) -> str:
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    msg = f"{timestamp_ms}\n{nonce}\n{method.upper()}\n{path_and_query}\n{body_hash}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(secret: str, method: str, path_and_query: str, body: str, now_ms: Optional[int] = None, nonce: Optional[str] = None) -> dict:
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    n = nonce or secrets.token_hex(16)  # 32 chars of [0-9a-f]; site wants 16-64 of [A-Za-z0-9_-]
    return {
        "content-type": "application/json",
        "x-ga-timestamp": ts,
        "x-ga-nonce": n,
        "x-ga-signature": sign(secret, ts, n, method, path_and_query, body),
    }


# ── review button / modal helpers ───────────────────────────────────────────
def parse_review_custom_id(custom_id: str) -> Optional[tuple[str, int]]:
    m = REVIEW_CUSTOM_ID.match(custom_id or "")
    return (m.group(1), int(m.group(2))) if m else None


def clean_reason(action: str, raw: str) -> tuple[Optional[str], Optional[str]]:
    """-> (reason, error). Reason is required for request_changes/reject; length-checked like the site does."""
    text = " ".join((raw or "").split())
    if action not in NEEDS_REASON:
        return (text[:REASON_MAX] or None), None
    if len(text) < REASON_MIN:
        return None, f"Please give a reason of at least {REASON_MIN} characters."
    if len(text) > REASON_MAX:
        return None, f"Please keep the reason under {REASON_MAX} characters."
    return text, None


def review_payload(listing_id: int, action: str, reason: Optional[str], actor_discord_id: int, correlation_id: Optional[str] = None) -> dict:
    p = {"listingId": listing_id, "action": action, "actorDiscordId": str(actor_discord_id), "correlationId": correlation_id or secrets.token_hex(16)}
    if reason:
        p["reason"] = reason
    return p


def describe_review_result(status: int, data: dict, action: str) -> str:
    """Ephemeral reply text. Site errors are shown as the site wrote them; anything unexpected stays generic."""
    if status == 200 and data.get("ok"):
        return {"approve": "Approved.", "request_changes": "Changes requested.", "reject": "Rejected."}.get(action, "Done.")
    err = data.get("error") if isinstance(data, dict) else None
    if status in (400, 403, 409, 503) and isinstance(err, str) and err:
        return err[:300]
    return "The site could not be reached or returned an unexpected answer. Nothing was confirmed; try again in a moment."


# ── HTTP client ─────────────────────────────────────────────────────────────
def is_disguild_reference(reference) -> bool:
    return isinstance(reference, str) and reference.startswith(DISGUILD_REF_PREFIX) and 4 <= len(reference) <= 80


def ping_payload(fields: dict) -> dict:
    """Only the fields the site reads, as strings (the site ignores everything else anyway)."""
    return {"fields": {k: str(fields[k])[:300] for k in PING_FIELDS if fields.get(k) is not None}}


def is_refund_ping(fields: dict) -> bool:
    flag = lambda k: str(fields.get(k, "")).lower() == "true"
    return flag("refunded") or flag("disputed") or flag("chargebacked") or fields.get("resource_name") == "refund"


def interpret_ping_reply(status: int, data: dict) -> tuple[int, str]:
    """(code, message) for Gumroad. Anything but a clean answer from the site becomes 503 so Gumroad retries the ping."""
    if status == 200 and data.get("ok") is True and isinstance(data.get("code"), int) and isinstance(data.get("msg"), str):
        return data["code"], data["msg"][:100]
    return 503, "disguild unavailable"


async def route_gumroad_ping(fields: dict, settings: "Settings", verify_sale, client=None) -> Optional[tuple[int, str]]:
    """Decide what to do with a Gumroad ping for Disguild.

    Returns None when the ping is not Disguild's (the caller carries on with the bot's own handling). Otherwise forwards it to the site and
    returns (code, message). A refund/dispute without a Disguild reference is forwarded best effort (the site matches it by sale id) and
    still returns None so the bot's own refund handling runs. `verify_sale(sale_id)` is awaited for real, paid, non-test sales first.
    """
    reference = fields.get("url_params[reference]") or fields.get("reference")
    ours = is_disguild_reference(reference)
    refund = is_refund_ping(fields)
    if not ours and not refund:
        return None
    if not settings.enabled:
        return (503, "disguild unavailable") if ours else None
    client = client or SiteClient(settings)
    is_test = str(fields.get("test", "")).lower() == "true"
    if ours and not refund and not is_test and not await verify_sale(str(fields.get("sale_id", ""))):
        return 200, "sale not verified"
    status, data = await client.gumroad_ping(fields)
    code, msg = interpret_ping_reply(status, data)
    if ours:
        return code, msg
    logger.info("gumroad refund forwarded to the site for sale %s: %s", fields.get("sale_id", "?"), msg)
    return None


class SiteClient:
    def __init__(self, settings: Settings):
        self.s = settings

    async def _send(self, method: str, path: str, body: str = "", signed: bool = True, extra: Optional[dict] = None, timeout: float = 15) -> tuple[int, dict, dict]:
        headers = signed_headers(self.s.secret, method, path, body) if signed else {"accept": "application/json"}
        if extra:
            headers.update(extra)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(method, self.s.site_url + path, data=body if method != "GET" else None, headers=headers) as r:
                    raw = await r.text()
                    try:
                        data = json.loads(raw) if raw else {}
                    except ValueError:
                        data = {}
                    return r.status, (data if isinstance(data, dict) else {}), dict(r.headers)
        except Exception as exc:  # network, DNS, timeout: report as status 0, never raise into a task loop
            logger.warning("guildatlas request failed: %s %s (%s)", method, path, type(exc).__name__)
            return 0, {}, {}

    async def review_action(self, payload: dict) -> tuple[int, dict]:
        status, data, _ = await self._send("POST", "/api/internal/review-action", json.dumps(payload, separators=(",", ":")))
        return status, data

    async def gumroad_ping(self, fields: dict) -> tuple[int, dict]:
        status, data, _ = await self._send("POST", "/api/internal/payments/ping", json.dumps(ping_payload(fields), separators=(",", ":")))
        return status, data

    async def drain_outbox(self) -> tuple[int, dict]:
        status, data, _ = await self._send("POST", "/api/internal/outbox/drain", "{}")
        return status, data

    async def internal_health(self) -> tuple[int, dict]:
        status, data, _ = await self._send("GET", "/api/internal/health")
        return status, data

    async def push_presence(self, added: list, removed: list) -> bool:
        body = json.dumps({"added": [str(i) for i in added], "removed": [str(i) for i in removed]}, separators=(",", ":"))
        status, data, _ = await self._send("POST", "/api/internal/guild-presence", body)
        return status == 200 and bool(data.get("ok"))

    async def push_stats(self, entries: list) -> bool:
        body = json.dumps({"bots": entries}, separators=(",", ":"))
        status, data, _ = await self._send("POST", "/api/internal/bot-stats", body)
        return status == 200 and bool(data.get("ok"))

    async def listed_servers(self) -> list:
        """Ids of approved server listings (the site's answer to: which servers do you want member counts for)."""
        status, data, _ = await self._send("GET", "/api/internal/listed-servers")
        servers = data.get("servers") if status == 200 else None
        return [int(x) for x in servers if isinstance(x, str) and SNOWFLAKE.match(x)] if isinstance(servers, list) else []

    async def push_guild_stats(self, entries: list) -> bool:
        body = json.dumps({"servers": entries}, separators=(",", ":"))
        status, data, _ = await self._send("POST", "/api/internal/guild-stats", body)
        return status == 200 and bool(data.get("ok"))

    async def pending_invites(self) -> list:
        status, data, _ = await self._send("GET", "/api/internal/invite-requests")
        pending = data.get("pending") if status == 200 else None
        return [int(x) for x in pending if isinstance(x, str) and SNOWFLAKE.match(x)] if isinstance(pending, list) else []

    async def claim_invite(self, guild_id: int) -> bool:
        status, data, _ = await self._send("POST", "/api/internal/invite-requests/claim", json.dumps({"guildId": str(guild_id)}, separators=(",", ":")))
        return status == 200 and data.get("claimed") is True

    async def complete_invite(self, guild_id: int, code: Optional[str] = None, error: Optional[str] = None) -> bool:
        payload = {"guildId": str(guild_id), **({"code": code} if code else {"error": (error or "unknown")[:200]})}
        status, data, _ = await self._send("POST", "/api/internal/invite-requests/complete", json.dumps(payload, separators=(",", ":")))
        return status == 200 and bool(data.get("applied"))

    async def web_events(self, after: Optional[int]) -> tuple[bool, list, int]:
        """-> (ok, events, latest). after=None is the first call of a new reader: no events, only the newest id."""
        path = "/api/internal/web-events" + (f"?after={int(after)}" if after is not None else "")
        status, data, _ = await self._send("GET", path)
        events = data.get("events") if status == 200 else None
        latest = data.get("latest")
        if status != 200 or not data.get("ok") or not isinstance(events, list) or isinstance(latest, bool) or not isinstance(latest, int):
            return False, [], 0
        return True, [e for e in events if isinstance(e, dict) and isinstance(e.get("id"), int)], latest

    async def queue_for(self, discord_id) -> dict:
        """The asker's own queue numbers, or {} on any failure (the AI then simply has no queue facts and must say so)."""
        did = str(discord_id)
        if not SNOWFLAKE.match(did):
            return {}
        status, data, _ = await self._send("GET", f"/api/internal/queue?discord_id={did}")
        return data if status == 200 and data.get("ok") else {}

    async def public_health(self) -> bool:
        status, data, _ = await self._send("GET", "/api/health", signed=False, timeout=10)
        return status == 200 and bool(data.get("ok"))

    async def knowledge(self, etag: Optional[str]) -> tuple[int, dict, Optional[str]]:
        status, data, headers = await self._send("GET", "/api/knowledge", signed=False, extra={"If-None-Match": etag} if etag else None)
        new_etag = next((v for k, v in headers.items() if k.lower() == "etag"), None)
        return status, data, new_etag


# ── site-health monitor (alerts with threshold, cooldown, recovery) ─────────
@dataclass
class HealthMonitor:
    fail_threshold: int = 3
    cooldown_seconds: int = 900
    _fails: int = 0
    _alerted_at: Optional[float] = None
    _last_internal: dict = field(default_factory=dict)

    def record_public(self, ok: bool, now: float) -> Optional[str]:
        """Returns a message to send, or None. Down alert after N consecutive failures, repeated at most per cooldown."""
        if ok:
            was_alerting = self._alerted_at is not None
            self._fails, self._alerted_at = 0, None
            return "GuildAtlas site is reachable again." if was_alerting else None
        self._fails += 1
        if self._fails < self.fail_threshold:
            return None
        if self._alerted_at is None or now - self._alerted_at >= self.cooldown_seconds:
            self._alerted_at = now
            return f"GuildAtlas site health check has failed {self._fails} times in a row."
        return None

    def internal_problems(self, h: dict, stale_after_seconds: int = 1800) -> list[str]:
        problems = []
        dead = (h.get("outbox") or {}).get("dead", 0)
        if dead:
            problems.append(f"{dead} review notification(s) permanently failed to deliver.")
        if int(h.get("oldestUndeliveredAgeSeconds") or 0) > stale_after_seconds:
            problems.append("Review notifications have been waiting a long time (check the review channel permissions).")
        if h.get("ok") is False:
            problems.append("The site reports a database problem.")
        return problems

    def internal_alert(self, h: dict, now: float) -> Optional[str]:
        """Same cooldown idea, keyed on the set of problems so a new problem alerts at once."""
        problems = self.internal_problems(h)
        key = "|".join(problems)
        last = self._last_internal
        if not problems:
            self._last_internal = {}
            return None
        if last.get("key") == key and now - last.get("at", 0) < self.cooldown_seconds * 4:
            return None
        self._last_internal = {"key": key, "at": now}
        return "GuildAtlas needs attention:\n- " + "\n- ".join(problems)


# ── AI knowledge cache ──────────────────────────────────────────────────────
SITE_QUESTION = re.compile(r"guild\s?atlas|(submit|listing|listed|directory|approved|pending|rejected|reviewer|moderation)\b.*\b(bot|server)\b|\b(bot|server)\b.*\b(submit|listing|listed|directory|approved|pending|rejected|reviewer|moderation)\b", re.I | re.S)

AI_POLICY = (
    "Answer GuildAtlas facts ONLY from the knowledge below. If it is not there, say you do not know or that it is not available yet. "
    "Never invent features, prices, payment status, approval decisions, analytics or account data. "
    "You cannot see reviewer identities or private reviewer notes. You only know a person's listing status or queue number when it is given to you below as their own queue facts; otherwise send them to their GuildAtlas dashboard. "
    "Never discuss tokens, secrets or infrastructure."
)
MAX_CONTEXT_CHARS = 9000


def is_site_question(text: str) -> bool:
    return bool(text) and bool(SITE_QUESTION.search(text))


def build_context(knowledge: dict, site_url: str = "") -> str:
    """Whole public knowledge as prompt text (it is small, so no fragile retrieval), capped."""
    parts = [f"{AI_POLICY}\n\nGuildAtlas knowledge (version {knowledge.get('version', '?')}):"]
    for s in knowledge.get("sections", []):
        if isinstance(s, dict) and isinstance(s.get("title"), str) and isinstance(s.get("body"), str):
            parts.append(f"## {s['title']}\n{s['body']}")
    if site_url:
        parts.append(f"Dashboard: {site_url}/dashboard")
    return "\n\n".join(parts)[:MAX_CONTEXT_CHARS]


class KnowledgeCache:
    def __init__(self, client: Optional[SiteClient], ttl_seconds: int = 3 * 3600):
        self.client, self.ttl = client, ttl_seconds
        self.data: dict = {}
        self.etag: Optional[str] = None
        self.fetched_at: float = 0.0

    @property
    def version(self) -> Optional[str]:
        return self.data.get("version")

    async def refresh(self, now: Optional[float] = None) -> bool:
        """True if the knowledge changed. 304 or any failure keeps the previous copy."""
        if not self.client:
            return False
        status, data, etag = await self.client.knowledge(self.etag)
        now = time.time() if now is None else now
        if status == 304:
            self.fetched_at = now
            return False
        if status == 200 and isinstance(data.get("sections"), list) and data.get("version"):
            changed = data["version"] != self.version
            self.data, self.etag, self.fetched_at = data, etag, now
            return changed
        logger.warning("guildatlas knowledge refresh failed (status %s); keeping previous copy", status)
        return False

    async def context_for(self, text: str, now: Optional[float] = None) -> str:
        """Prompt addition for a site question, or '' (not a site question, integration off, or nothing cached)."""
        if not is_site_question(text) or not self.client:
            return ""
        now = time.time() if now is None else now
        if not self.data or now - self.fetched_at > self.ttl:
            await self.refresh(now)
        return build_context(self.data, self.client.s.site_url) if self.data else ""


async def queue_context_for(text: str, discord_id) -> str:
    """Prompt addition when a person asks about the review queue: THEIR OWN numbers, fetched live. '' when it is not a queue question."""
    client = knowledge_cache.client
    if not client or not is_queue_question(text):
        return ""
    data = await client.queue_for(discord_id)
    if not data:
        return ("The person is asking about the Disguild review queue, but the site could not be reached just now. "
                "Do not guess a number: say you cannot see it right now and point them to their dashboard.")
    return queue_context(data, client.s.site_url)


# Shared instance. The cog sets .client when the integration is configured; ai_features reads it.
knowledge_cache = KnowledgeCache(None)
