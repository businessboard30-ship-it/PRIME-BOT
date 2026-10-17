"""GuildAtlas integration: signing parity with the site, review helpers, health alerts, AI knowledge rules."""
import asyncio
import json
import pathlib

import pytest

from modules import guildatlas as ga

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "guildatlas_knowledge.json"
KNOWLEDGE = json.loads(FIXTURE.read_text())


def run(coro):
    return asyncio.run(coro)


# ── signing ─────────────────────────────────────────────────────────────────
def test_signature_matches_the_sites_node_reference():
    # Vector produced by the GuildAtlas reference signer (node crypto, same canonical string).
    sig = ga.sign("s" * 32, "1700000000000", "abcdef0123456789abcdef0123456789", "post", "/api/internal/review-action", '{"a":1}')
    assert sig == "7986b46ea8160ae1b4d2ad963ffa0c3b05d5d1dc6e5a3ae984ce005d32261e7f"


def test_headers_meet_the_sites_format_rules():
    import re
    h = ga.signed_headers("s" * 32, "POST", "/x", "{}")
    assert re.fullmatch(r"\d{10,15}", h["x-ga-timestamp"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", h["x-ga-nonce"])
    assert re.fullmatch(r"[0-9a-f]{64}", h["x-ga-signature"])


def test_nonces_are_not_reused():
    assert len({ga.signed_headers("s" * 32, "GET", "/x", "")["x-ga-nonce"] for _ in range(50)}) == 50


# ── settings ────────────────────────────────────────────────────────────────
def test_integration_is_off_without_url_or_with_short_secret():
    assert not ga.load_settings({}).enabled
    assert not ga.load_settings({"GUILDATLAS_SITE_URL": "https://x.y", "GUILDATLAS_INTERNAL_SECRET": "short"}).enabled
    s = ga.load_settings({"GUILDATLAS_SITE_URL": "https://x.y/", "GUILDATLAS_INTERNAL_SECRET": "z" * 32, "GUILDATLAS_ALERT_CHANNEL_ID": "123456789012345678"})
    assert s.enabled and s.site_url == "https://x.y" and s.alert_channel_id == 123456789012345678


def test_bad_alert_channel_id_is_ignored():
    assert ga.load_settings({"GUILDATLAS_ALERT_CHANNEL_ID": "12; DROP"}).alert_channel_id == 0


# ── review custom ids, reasons, payloads, replies ───────────────────────────
@pytest.mark.parametrize("cid,expected", [
    ("ga:r:approve:12", ("approve", 12)),
    ("ga:r:request_changes:1", ("request_changes", 1)),
    ("ga:r:reject:999999999999", ("reject", 999999999999)),
])
def test_valid_custom_ids(cid, expected):
    assert ga.parse_review_custom_id(cid) == expected


@pytest.mark.parametrize("cid", ["", "ga:r:suspend:1", "ga:r:approve:", "ga:r:approve:1x", "ga:r:approve:1234567890123", "xga:r:approve:1", "ga:r:approve:1\nx", "other:thing"])
def test_invalid_custom_ids_are_ignored(cid):
    assert ga.parse_review_custom_id(cid) is None


def test_reason_rules():
    assert ga.clean_reason("approve", "") == (None, None)
    assert ga.clean_reason("reject", "  too   short ") == ("too short", None)
    assert ga.clean_reason("reject", "no")[0] is None
    assert ga.clean_reason("request_changes", "x" * 501)[0] is None
    assert ga.clean_reason("reject", "a good reason")[0] == "a good reason"


def test_payload_uses_the_clicking_user_as_actor():
    p = ga.review_payload(7, "reject", "bad description", 111222333444555666)
    assert p["actorDiscordId"] == "111222333444555666" and p["listingId"] == 7 and p["reason"] == "bad description" and p["correlationId"]
    assert "reason" not in ga.review_payload(7, "approve", None, 1)


def test_reply_text_never_claims_success_on_failure():
    assert ga.describe_review_result(200, {"ok": True}, "approve") == "Approved."
    assert ga.describe_review_result(403, {"ok": False, "error": "You are not an authorized reviewer."}, "approve") == "You are not an authorized reviewer."
    assert ga.describe_review_result(0, {}, "approve").startswith("The site could not be reached")
    assert ga.describe_review_result(500, {"error": "stack trace secrets"}, "approve").startswith("The site could not be reached")
    assert ga.describe_review_result(200, {"ok": False}, "approve").startswith("The site could not be reached")


# ── health alerts ───────────────────────────────────────────────────────────
def test_down_alert_needs_three_failures_then_cools_down_then_recovers():
    m = ga.HealthMonitor()
    assert m.record_public(False, 0) is None and m.record_public(False, 60) is None
    assert "failed 3 times" in m.record_public(False, 120)
    assert m.record_public(False, 180) is None            # cooldown
    assert m.record_public(False, 120 + 900) is not None   # repeats after cooldown
    assert "reachable again" in m.record_public(True, 2000)
    assert m.record_public(True, 2060) is None


def test_a_blip_does_not_alert():
    m = ga.HealthMonitor()
    m.record_public(False, 0); m.record_public(False, 60)
    assert m.record_public(True, 120) is None
    assert m.record_public(False, 180) is None


def test_internal_problems_and_dedupe():
    m = ga.HealthMonitor()
    assert m.internal_alert({"ok": True, "outbox": {"sent": 5}, "oldestUndeliveredAgeSeconds": 0}, 0) is None
    bad = {"ok": True, "outbox": {"dead": 2}, "oldestUndeliveredAgeSeconds": 0}
    assert "2 review notification" in m.internal_alert(bad, 0)
    assert m.internal_alert(bad, 60) is None                       # same problem, within cooldown
    worse = {"ok": True, "outbox": {"dead": 2}, "oldestUndeliveredAgeSeconds": 4000}
    assert "waiting a long time" in m.internal_alert(worse, 120)   # new problem alerts at once
    assert m.internal_alert({"ok": True, "outbox": {}}, 200) is None


# ── AI knowledge ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("how do I submit my bot to GuildAtlas?", True),
    ("guild atlas is my bot approved", True),
    ("why is my server listing still pending", True),
    ("who is the best anime character", False),
    ("play some music", False),
    ("", False),
])
def test_site_question_detection(text, expected):
    assert ga.is_site_question(text) is expected


def test_context_contains_policy_and_every_section_and_respects_cap():
    ctx = ga.build_context(KNOWLEDGE, "https://site.example")
    assert "ONLY from the knowledge below" in ctx and "dashboard" in ctx.lower()
    assert all(s["title"] in ctx for s in KNOWLEDGE["sections"])
    assert len(ctx) <= ga.MAX_CONTEXT_CHARS
    assert len(ga.build_context({"sections": [{"title": "t", "body": "x" * 50000}]})) <= ga.MAX_CONTEXT_CHARS


def test_every_site_eval_is_answerable_from_the_injected_context():
    """Retrieval check only: the facts each eval requires must be in the prompt. (It does not run the LLM.)"""
    ctx = ga.build_context(KNOWLEDGE, "https://site.example")
    for ev in KNOWLEDGE["evals"]:
        for needle in ev["mustInclude"]:
            assert needle in ctx, f"eval {ev['id']}: {needle!r} missing from context"


def test_policy_sensitive_evals_have_the_not_available_section():
    ctx = ga.build_context(KNOWLEDGE)
    assert "NOT exist yet" in ctx and "voting" in ctx


def test_context_has_no_secret_looking_content():
    ctx = ga.build_context(KNOWLEDGE, "https://site.example").lower()
    for bad in ("secret_key", "bearer ", "ghp_", "internal_api_secret=", "password"):
        assert bad not in ctx


class FakeClient:
    class S:
        site_url = "https://site.example"
    s = S()

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def knowledge(self, etag):
        self.calls.append(etag)
        return self.replies.pop(0)


def test_cache_fetches_sends_etag_and_keeps_old_copy_on_failure():
    c = FakeClient([(200, KNOWLEDGE, '"v1"'), (304, {}, None), (0, {}, None), (200, {"version": "v2", "sections": []}, '"v2"'), (500, {}, None)])
    cache = ga.KnowledgeCache(c)
    assert run(cache.refresh(now=1)) is True and cache.version == KNOWLEDGE["version"]
    assert run(cache.refresh(now=2)) is False and c.calls[-1] == '"v1"'      # conditional request, 304 keeps data
    assert run(cache.refresh(now=3)) is False and cache.version == KNOWLEDGE["version"]  # network failure keeps data
    assert run(cache.refresh(now=4)) is True and cache.version == "v2"        # new version detected
    assert run(cache.refresh(now=5)) is False and cache.version == "v2"       # server error keeps data


def test_cache_returns_context_only_for_site_questions():
    cache = ga.KnowledgeCache(FakeClient([(200, KNOWLEDGE, '"v1"')]))
    assert run(cache.context_for("play some music")) == ""
    assert "Disguild knowledge" in run(cache.context_for("how do I submit my bot to GuildAtlas?", now=10))


def test_no_client_means_no_context_and_no_crash():
    cache = ga.KnowledgeCache(None)
    assert run(cache.context_for("how do I submit my bot to GuildAtlas?")) == ""
    assert run(cache.refresh()) is False


# ── clones and server invites ───────────────────────────────────────────────
def test_chunking_respects_the_sites_limit_of_300_ids():
    parts = list(ga.chunked(range(650)))
    assert [len(p) for p in parts] == [300, 300, 50]
    assert list(ga.chunked([])) == []


def test_presence_resends_everything_and_reports_what_disappeared():
    added, removed = ga.presence_delta({1, 2, 3}, {2, 3, 4})
    assert added == [2, 3, 4]  # everything current is refreshed so the site's 3-hour freshness never lapses
    assert removed == [1]


def test_invite_codes_are_validated_before_being_sent_to_the_site():
    assert ga.invite_code_from("abcDEF12") == "abcDEF12"
    assert ga.invite_code_from(" ab-12 ") == "ab-12"
    assert ga.invite_code_from("bad code!") is None
    assert ga.invite_code_from(None) is None
    assert ga.invite_code_from("x" * 40) is None


def test_site_client_calls_use_the_right_paths_and_payloads():
    calls = []

    class C(ga.SiteClient):
        async def _send(self, method, path, body="", signed=True, extra=None, timeout=15):
            calls.append((method, path, json.loads(body) if body else None))
            if path.endswith("guild-presence"):
                return 200, {"ok": True}, {}
            if path.endswith("invite-requests") and method == "GET":
                return 200, {"ok": True, "pending": ["123456789012345678", "bad", 5]}, {}
            if path.endswith("claim"):
                return 200, {"ok": True, "claimed": True}, {}
            return 200, {"ok": True, "applied": True}, {}

    c = C(ga.Settings("https://x", "s" * 32, 0))
    assert run(c.push_presence([111111111111111111], [])) is True
    assert run(c.pending_invites()) == [123456789012345678]
    assert run(c.claim_invite(123456789012345678)) is True
    assert run(c.complete_invite(123456789012345678, code="abcDEF12")) is True
    assert run(c.complete_invite(123456789012345678, error="x" * 500)) is True
    assert calls[0] == ("POST", "/api/internal/guild-presence", {"added": ["111111111111111111"], "removed": []})
    assert calls[1][:2] == ("GET", "/api/internal/invite-requests")
    assert calls[3] == ("POST", "/api/internal/invite-requests/complete", {"guildId": "123456789012345678", "code": "abcDEF12"})
    assert len(calls[4][2]["error"]) == 200


def test_stats_entry_accepts_only_what_the_site_accepts():
    assert ga.stats_entry(123456789012345678, 42) == {"applicationId": "123456789012345678", "serverCount": 42}
    assert ga.stats_entry("123456789012345678", 0, 3) == {"applicationId": "123456789012345678", "serverCount": 0, "shardCount": 3}
    for bad in [(None, 1), ("abc", 1), (123456789012345678, -1), (123456789012345678, 1.5), (123456789012345678, True),
                (123456789012345678, ga.MAX_SERVERS + 1), (123456789012345678, 1, 0), (123456789012345678, 1, ga.MAX_SHARDS + 1)]:
        assert ga.stats_entry(*bad) is None


def test_push_stats_posts_to_the_bot_stats_path():
    calls = []

    class C(ga.SiteClient):
        async def _send(self, method, path, body="", signed=True, extra=None, timeout=15):
            calls.append((method, path, json.loads(body)))
            return 200, {"ok": True, "stored": 1}, {}

    c = C(ga.Settings("https://x", "s" * 32, 0))
    entry = ga.stats_entry(123456789012345678, 7, 2)
    assert run(c.push_stats([entry])) is True
    assert calls == [("POST", "/api/internal/bot-stats", {"bots": [entry]})]

    class Down(ga.SiteClient):
        async def _send(self, *a, **k):
            return 0, {}, {}

    assert run(Down(ga.Settings("https://x", "s" * 32, 0)).push_stats([entry])) is False


# ── Disguild payments forwarding (this bot owns the single Gumroad Ping URL) ─
ENABLED = ga.Settings(site_url="https://site.example", secret="s" * 32, alert_channel_id=0)
DISABLED = ga.Settings(site_url="", secret="", alert_channel_id=0)


class PingClient:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.sent = status, data if data is not None else {"ok": True, "code": 200, "msg": "ok"}, []

    async def gumroad_ping(self, fields):
        self.sent.append(fields)
        return self.status, self.data


def verifier(result=True):
    calls = []

    async def verify(sale_id):
        calls.append(sale_id)
        return result
    verify.calls = calls
    return verify


def route(fields, settings=ENABLED, client=None, verify=None):
    return run(ga.route_gumroad_ping(fields, settings, verify or verifier(), client or PingClient()))


def test_only_disguild_references_are_recognised():
    assert ga.is_disguild_reference("ga_" + "ab" * 16)
    for bad in (None, "", "ga_", "prime_123", "GA_abcdef", 42, "ga_" + "x" * 90):
        assert not ga.is_disguild_reference(bad)


def test_payload_keeps_only_the_fields_the_site_reads():
    p = ga.ping_payload({"sale_id": "s1", "price": 500, "email": "a@b.c", "url_params[reference]": "ga_abc1", "card_bin": "4242", "test": None})
    assert p == {"fields": {"sale_id": "s1", "price": "500", "url_params[reference]": "ga_abc1"}}
    assert len(ga.ping_payload({"sale_id": "x" * 1000})["fields"]["sale_id"]) == 300


def test_refund_detection():
    assert ga.is_refund_ping({"refunded": "true"}) and ga.is_refund_ping({"disputed": "True"}) and ga.is_refund_ping({"chargebacked": "true"})
    assert ga.is_refund_ping({"resource_name": "refund"})
    assert not ga.is_refund_ping({"refunded": "false", "sale_id": "s"})


def test_reply_interpretation_retries_on_anything_unclean():
    assert ga.interpret_ping_reply(200, {"ok": True, "code": 200, "msg": "ok"}) == (200, "ok")
    assert ga.interpret_ping_reply(200, {"ok": True, "code": 503, "msg": "try again"}) == (503, "try again")
    for status, data in ((0, {}), (401, {"ok": False}), (500, {}), (200, {}), (200, {"ok": True, "code": "200", "msg": "ok"}), (400, {"ok": False, "error": "bad_body"})):
        assert ga.interpret_ping_reply(status, data) == (503, "disguild unavailable")


def test_bots_own_pings_are_left_alone():
    client = PingClient()
    assert route({"sale_id": "s", "url_params[reference]": "prime_99"}, client=client) is None
    assert route({"sale_id": "s"}, client=client) is None
    assert client.sent == []


def test_disguild_sale_is_verified_then_forwarded():
    client, verify = PingClient(), verifier(True)
    assert route({"sale_id": "s1", "price": "500", "url_params[reference]": "ga_abc1"}, client=client, verify=verify) == (200, "ok")
    assert verify.calls == ["s1"] and client.sent[0]["sale_id"] == "s1"


def test_unverified_disguild_sale_is_not_forwarded():
    client = PingClient()
    assert route({"sale_id": "s1", "url_params[reference]": "ga_abc1"}, client=client, verify=verifier(False)) == (200, "sale not verified")
    assert client.sent == []


def test_test_purchases_skip_verification_and_the_site_decides():
    client, verify = PingClient(data={"ok": True, "code": 200, "msg": "test ping ignored"}), verifier(False)
    assert route({"sale_id": "s1", "test": "true", "url_params[reference]": "ga_abc1"}, client=client, verify=verify) == (200, "test ping ignored")
    assert verify.calls == [] and len(client.sent) == 1


def test_site_outage_makes_gumroad_retry_a_disguild_sale():
    assert route({"sale_id": "s1", "url_params[reference]": "ga_abc1"}, client=PingClient(status=0, data={})) == (503, "disguild unavailable")
    assert route({"sale_id": "s1", "url_params[reference]": "ga_abc1"}, settings=DISABLED) == (503, "disguild unavailable")


def test_refund_for_a_disguild_order_is_forwarded_without_api_verification():
    client, verify = PingClient(data={"ok": True, "code": 200, "msg": "refund applied"}), verifier(False)
    assert route({"sale_id": "s1", "refunded": "true", "url_params[reference]": "ga_abc1"}, client=client, verify=verify) == (200, "refund applied")
    assert verify.calls == []


def test_refund_without_reference_is_forwarded_best_effort_and_the_bot_still_handles_it():
    client = PingClient(status=0, data={})
    assert route({"sale_id": "s1", "refunded": "true"}, client=client) is None   # the bot's own refund handling carries on
    assert len(client.sent) == 1
    assert route({"sale_id": "s1", "refunded": "true"}, settings=DISABLED) is None


def test_the_client_uses_the_signed_payments_endpoint():
    seen = {}

    class Spy(ga.SiteClient):
        async def _send(self, method, path, body="", signed=True, extra=None, timeout=15):
            seen.update(method=method, path=path, body=json.loads(body), signed=signed)
            return 200, {"ok": True, "code": 200, "msg": "ok"}, {}
    assert run(Spy(ENABLED).gumroad_ping({"sale_id": "s1", "email": "x@y.z"})) == (200, {"ok": True, "code": 200, "msg": "ok"})
    assert seen == {"method": "POST", "path": "/api/internal/payments/ping", "body": {"fields": {"sale_id": "s1"}}, "signed": True}


# ── member counts for listed servers (main bot and clones both report the servers they are in) ─
class _G:
    def __init__(self, id, member_count):
        self.id, self.member_count = id, member_count


def test_guild_stats_entries_only_listed_servers_this_process_is_in():
    guilds = [_G(300000000000000001, 120), _G(300000000000000002, 5), _G(300000000000000003, None), _G(300000000000000004, -1),
              _G(300000000000000005, True), _G(300000000000000006, ga.MAX_MEMBERS + 1)]
    listed = [300000000000000001, 300000000000000003, 300000000000000004, 300000000000000005, 300000000000000006, 300000000000000099]
    assert ga.guild_stats_entries(guilds, listed) == [{"guildId": "300000000000000001", "memberCount": 120}]
    assert ga.guild_stats_entries([], listed) == []
    assert ga.guild_stats_entries(guilds, []) == []
    assert ga.guild_stats_entries(guilds, ["300000000000000002", "junk", None]) == [{"guildId": "300000000000000002", "memberCount": 5}]


def test_listed_servers_and_push_guild_stats_use_the_site_paths():
    calls = []

    class C(ga.SiteClient):
        async def _send(self, method, path, body="", signed=True, extra=None, timeout=15):
            calls.append((method, path, json.loads(body) if body else None))
            if path.endswith("listed-servers"):
                return 200, {"ok": True, "servers": ["300000000000000001", "x", 5]}, {}
            return 200, {"ok": True, "stored": 1}, {}

    c = C(ga.Settings("https://x", "s" * 32, 0))
    assert run(c.listed_servers()) == [300000000000000001]
    entry = {"guildId": "300000000000000001", "memberCount": 7}
    assert run(c.push_guild_stats([entry])) is True
    assert calls == [("GET", "/api/internal/listed-servers", None), ("POST", "/api/internal/guild-stats", {"servers": [entry]})]

    class Down(ga.SiteClient):
        async def _send(self, *a, **k):
            return 0, {}, {}

    d = Down(ga.Settings("https://x", "s" * 32, 0))
    assert run(d.listed_servers()) == [] and run(d.push_guild_stats([entry])) is False


# ── web log and queue numbers (Disguild Phase 38) ───────────────────────────
def test_added_event_shows_the_queue_number():
    ev = {"id": 1, "kind": "listing.submitted", "at": "2026-10-17T10:00:00.000Z",
          "data": {"listing": {"id": 7, "type": "bot", "slug": "my-bot", "name": "My Bot", "owner": "sam"}, "queue": {"position": 3, "waiting": 5}}}
    e = ga.format_web_event(ev, "https://disguild.pages.dev/")
    assert e["title"] == "Bot added"
    assert "**#3**" in e["description"] and "5 waiting" in e["description"] and "My Bot" in e["description"]
    assert "https://disguild.pages.dev/bots/my-bot" in e["description"]


def test_events_without_identity_and_unknown_kinds():
    vote = ga.format_web_event({"id": 2, "kind": "vote", "data": {"listing": {"type": "server", "name": "Cool", "slug": "cool"}, "vote_count": 12}})
    assert vote["title"] == "New vote" and "12" in vote["description"]
    assert ga.format_web_event({"id": 3, "kind": "something.new", "data": {}}) is None
    assert ga.format_web_event("junk") is None
    bad_slug = ga.format_web_event({"id": 4, "kind": "listing.approved", "data": {"listing": {"type": "bot", "name": "X", "slug": "../x"}}}, "https://s")
    assert "https://s/" not in bad_slug["description"]


def test_queue_question_detection():
    for q in ("what is my bot's queue number?", "where am I in the queue", "what's my position in line", "how long until my bot is reviewed"):
        assert ga.is_queue_question(q), q
    for q in ("hello", "play some music", "tell me a joke"):
        assert not ga.is_queue_question(q), q


def test_queue_context_quotes_only_the_askers_numbers():
    ctx = ga.queue_context({"ok": True, "waiting": 9, "listings": [{"name": "My Bot", "type": "bot", "status": "pending", "position": 4},
                                                                       {"name": "Old", "type": "server", "status": "rejected", "position": None}]}, "https://s")
    assert "queue number 4 of 9" in ctx and "Old" in ctx and "rejected" in ctx and "https://s/dashboard" in ctx
    empty = ga.queue_context({"ok": True, "waiting": 2, "listings": []})
    assert "no listing waiting" in empty
    assert ga.queue_context({"ok": False}) == ""


def test_queue_context_for_is_off_without_the_integration_and_for_other_questions():
    old0 = ga.knowledge_cache.client
    ga.knowledge_cache.client = None
    try:
        assert run(ga.queue_context_for("what is my queue number", 123456789012345678)) == ""
    finally:
        ga.knowledge_cache.client = old0
    class Fake:
        s = ga.Settings("https://s", "x" * 32, 0)
        async def queue_for(self, did):
            return {"ok": True, "waiting": 1, "listings": [{"name": "B", "type": "bot", "status": "pending", "position": 1}]}
    old = ga.knowledge_cache.client
    ga.knowledge_cache.client = Fake()
    try:
        assert "queue number 1 of 1" in run(ga.queue_context_for("what is my bot's queue number?", 123456789012345678))
        assert run(ga.queue_context_for("tell me a joke", 123456789012345678)) == ""
    finally:
        ga.knowledge_cache.client = old
