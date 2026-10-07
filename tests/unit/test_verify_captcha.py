import time
from utils.verify_captcha import sign_challenge, parse_challenge

S = "unit-test-secret"


def test_roundtrip():
    t = sign_challenge(S, 1, 2, None)
    assert parse_challenge(S, t) == {"guild_id": 1, "user_id": 2, "clone_id": None, "nonce": parse_challenge(S, t)["nonce"]}


def test_clone_id_preserved():
    assert parse_challenge(S, sign_challenge(S, 1, 2, 7))["clone_id"] == 7


def test_rejects_wrong_secret_and_tampering():
    t = sign_challenge(S, 1, 2, None)
    assert parse_challenge("other", t) is None
    v, body, sig = t.split(".")
    assert parse_challenge(S, f"{v}.{body}x.{sig}") is None
    assert parse_challenge(S, "garbage") is None
    assert parse_challenge("", t) is None


def test_expiry():
    t = sign_challenge(S, 1, 2, None, now=time.time() - 1000, ttl=600)
    assert parse_challenge(S, t) is None


def test_nonces_unique():
    assert parse_challenge(S, sign_challenge(S, 1, 2, None))["nonce"] != parse_challenge(S, sign_challenge(S, 1, 2, None))["nonce"]
