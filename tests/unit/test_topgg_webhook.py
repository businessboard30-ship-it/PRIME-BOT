import hashlib
import hmac
import json

from api.topgg_webhook import authenticate, extract_vote, verify_v1_signature

SECRET = "whs_testsecret"
NOW = 1_800_000_000


def _sign(body: bytes, ts: int = NOW, secret: str = SECRET) -> str:
    sig = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_valid_v1_signature():
    body = b'{"type":"vote.create"}'
    assert verify_v1_signature(body, _sign(body), SECRET, now=NOW)


def test_v1_rejects_wrong_secret_tampered_body_and_garbage():
    body = b'{"type":"vote.create"}'
    assert not verify_v1_signature(body, _sign(body, secret="other"), SECRET, now=NOW)
    assert not verify_v1_signature(b'{"type":"x"}', _sign(body), SECRET, now=NOW)
    assert not verify_v1_signature(body, "garbage", SECRET, now=NOW)
    assert not verify_v1_signature(body, "", SECRET, now=NOW)
    assert not verify_v1_signature(body, _sign(body), "", now=NOW)


def test_v1_rejects_stale_timestamp():
    body = b"{}"
    assert not verify_v1_signature(body, _sign(body, ts=NOW - 3600), SECRET, now=NOW)


def test_authenticate_v0_header_and_no_downgrade():
    body = b"{}"
    assert authenticate({"Authorization": SECRET}, body, SECRET, now=NOW)
    assert not authenticate({"Authorization": "nope"}, body, SECRET, now=NOW)
    assert not authenticate({}, body, SECRET, now=NOW)
    # A bad v1 signature must not be rescued by a correct Authorization header.
    assert not authenticate(
        {"x-topgg-signature": "t=1,v1=bad", "Authorization": SECRET}, body, SECRET, now=NOW
    )
    assert not authenticate({"Authorization": ""}, body, "", now=NOW)


def test_extract_v1_vote_uses_discord_platform_id():
    payload = {"type": "vote.create", "data": {
        "id": "808499215864008704",
        "project": {"platform": "discord"},
        "user": {"id": "topgg-internal", "platform_id": "1234567890"},
    }}
    assert extract_vote(payload) == ("vote", 1234567890, "808499215864008704")


def test_extract_v0_vote_and_tests_and_junk():
    assert extract_vote({"type": "upvote", "user": "42"}) == ("vote", 42, None)
    assert extract_vote({"type": "test", "user": "42"})[0] == "test"
    assert extract_vote({"type": "webhook.test", "data": {}})[0] == "test"
    assert extract_vote({"type": "vote.create", "data": {"user": {"platform_id": "abc"}}})[0] == "ignore"
    assert extract_vote({"type": "vote.create", "data": {"project": {"platform": "telegram"},
                                                          "user": {"platform_id": "1"}}})[0] == "ignore"
    assert extract_vote({"type": "weird"})[0] == "ignore"
    assert extract_vote([])[0] == "ignore"


def test_thanks_message_mentions_boost_and_link():
    from api.topgg_webhook import build_thanks_message
    msg = build_thanks_message(1.5, 12.0, "https://top.gg/bot/1/vote")
    assert "1.5x XP" in msg and "12 hours" in msg and "https://top.gg/bot/1/vote" in msg
    assert "http" not in build_thanks_message(2.0, 6.0, "")


def test_notify_voter_never_raises_without_token():
    import asyncio
    from api.topgg_webhook import notify_voter
    assert asyncio.run(notify_voter(1, "hi", "")) is False


def test_voter_is_dmed_only_on_their_first_vote():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from api import topgg_webhook as w
    cfg = SimpleNamespace(TOPGG_VOTE_HOURS=12.0, TOPGG_VOTE_MULTIPLIER=1.5, TOPGG_VOTE_URL="", DISCORD_BOT_TOKEN="t")

    def vote(count):
        db = SimpleNamespace(record_topgg_vote=AsyncMock(return_value={"vote_count": count} if count else None))
        with patch.object(w, "notify_voter", AsyncMock(return_value=True)) as dm:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(w._record_and_thank(db, cfg, 5, "v"))
            finally:
                loop.close()
            return dm.await_count

    assert vote(1) == 1      # first vote: thank them
    assert vote(2) == 0      # repeat voter: recorded, no DM
    assert vote(9) == 0
    assert vote(0) == 0      # duplicate delivery: nothing
