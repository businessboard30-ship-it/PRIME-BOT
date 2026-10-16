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
    assert "GuildAtlas knowledge" in run(cache.context_for("how do I submit my bot to GuildAtlas?", now=10))


def test_no_client_means_no_context_and_no_crash():
    cache = ga.KnowledgeCache(None)
    assert run(cache.context_for("how do I submit my bot to GuildAtlas?")) == ""
    assert run(cache.refresh()) is False
