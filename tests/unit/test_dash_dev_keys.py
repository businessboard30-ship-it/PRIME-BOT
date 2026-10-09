"""Developer mode C3: bring your own AI key. Gate, step-up, validation, encryption, no key ever leaves, own-key chat."""
import importlib
import inspect
import json
import logging
import time
from pathlib import Path

import pytest

from api import dash, dash_dev
from modules import dev_keys
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  ("sid" = user 6, card plan only; "other" = dev_monthly)

SECRET = "sk-test-SECRETKEY1234567890abcd"
MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def keys(mem, monkeypatch):
    fake, seen = mem
    state = {"rows": {}, "validated": [], "chat": [], "valid": True, "ai_spent": 0, "chat_fail": None}

    async def lst(uid):
        return [{"provider": p, "last4": v["last4"], "created_at": None, "updated_at": None}
                for (u, p), v in sorted(state["rows"].items()) if u == uid]

    async def secret(uid, provider):
        v = state["rows"].get((uid, provider))
        return v["enc"] if v else None

    async def upsert(uid, provider, enc, last4):
        state["rows"][(uid, provider)] = {"enc": enc, "last4": last4}

    async def delete(uid, provider):
        return state["rows"].pop((uid, provider), None) is not None

    async def validate(provider, key):
        state["validated"].append((provider, key))
        return (True, None) if state["valid"] else (False, "Groq didn't accept that key.")

    async def chat(provider, key, system, messages):
        state["chat"].append((provider, key))
        if state["chat_fail"]:
            raise RuntimeError(state["chat_fail"])
        return "own answer"

    async def consume(uid, ws, src, limit):
        state["ai_spent"] += 1
        return 1
    for n, f in (("dev_connection_list", lst), ("dev_connection_secret", secret), ("dev_connection_upsert", upsert),
                 ("dev_connection_delete", delete), ("ai_usage_consume", consume)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(dev_keys, "validate", validate)
    monkeypatch.setattr(dev_keys, "chat", chat)
    dash._owner_hits.clear()
    return fake, state


def fresh(fake, token="other"):
    fake.sessions[token]["fresh_until"] = int(time.time()) + 300


def save(token="other", provider="groq", key=SECRET):
    return call("POST", None, token=token, body={"action": "dev_key_save", "provider": provider, "key": key})


# ---------- gate + step-up ----------
def test_keys_routes_are_gated_for_non_subscribers(keys):
    fake, state = keys
    assert call("GET", {"action": "dev_keys"}, token="sid")[0] == 402
    fresh(fake, "sid")
    st, p, _ = save(token="sid")
    assert st == 402 and p["code"] == "subscription_required" and state["rows"] == {} and state["validated"] == []
    st, p, _ = call("POST", None, token="sid", body={"action": "dev_stepup"})
    assert st == 402


def test_saving_needs_a_fresh_sign_in(keys):
    fake, state = keys
    st, p, _ = save()
    assert st == 403 and p["code"] == "stepup_required" and state["validated"] == [] and state["rows"] == {}
    fake.sessions["other"]["fresh_until"] = int(time.time()) - 5                  # expired step-up
    assert save()[0] == 403
    st, p, _ = call("POST", None, token="other", body={"action": "dev_key_remove", "provider": "groq"})
    assert st == 403 and p["code"] == "stepup_required"


def test_step_up_start_returns_a_discord_url_and_binds_the_session(keys, monkeypatch):
    fake, _ = keys
    monkeypatch.setattr(dash, "_check_oauth_configured", lambda: None)
    states = {}

    async def create(state, return_to=None):
        states[state] = return_to
    monkeypatch.setattr(fake, "create_login_oauth_state", create, raising=False)
    st, p, _ = call("POST", None, token="other", body={"action": "dev_stepup"})
    assert st == 200 and p["url"].startswith("https://discord.com/") and list(states.values()) == ["dash_stepup_dev:other"]


# ---------- save / replace / remove ----------
def test_save_validates_then_stores_ciphertext_and_never_returns_the_key(keys):
    fake, state = keys
    fresh(fake)
    st, p, _ = save()
    assert st == 200 and state["validated"] == [("groq", SECRET)]
    stored = state["rows"][("7777777777", "groq")]
    assert stored["enc"] != SECRET and SECRET not in stored["enc"] and stored["last4"] == SECRET[-4:]
    from utils.crypto import secret_manager
    assert secret_manager.decrypt(stored["enc"]) == SECRET
    assert p["connections"][0]["provider"] == "groq" and p["connections"][0]["last4"] == SECRET[-4:]
    assert SECRET not in json.dumps(p)


def test_a_key_the_provider_rejects_is_not_stored(keys):
    fake, state = keys
    fresh(fake)
    state["valid"] = False
    st, p, _ = save()
    assert st == 422 and state["rows"] == {} and SECRET not in json.dumps(p)


def test_bad_provider_and_bad_key_shapes_are_rejected_before_any_call(keys):
    fake, state = keys
    fresh(fake)
    assert save(provider="evil")[0] == 422
    for bad in ("", "short", "has spaces in it which is not a key at all", "x" * 301, None, 123, "key\nwith\nnewline-aaaaaaaa"):
        dash._owner_hits.clear()
        assert save(key=bad)[0] == 422
    assert state["validated"] == [] and state["rows"] == {}


def test_replace_overwrites_one_row_and_remove_deletes_it(keys):
    fake, state = keys
    fresh(fake)
    assert save()[0] == 200
    new = "sk-test-NEWKEY0987654321zzzz"
    st, p, _ = save(key=new)
    assert st == 200 and len(state["rows"]) == 1 and p["connections"][0]["last4"] == "zzzz"
    st, p, _ = call("POST", None, token="other", body={"action": "dev_key_remove", "provider": "groq"})
    assert st == 200 and p["connections"] == [] and state["rows"] == {}


def test_list_returns_provider_last4_and_dates_only(keys):
    fake, state = keys
    fresh(fake)
    save(provider="openai")
    st, p, _ = call("GET", {"action": "dev_keys"}, token="other")
    assert st == 200 and set(p["connections"][0]) == {"provider", "label", "last4", "added_at", "updated_at"}
    assert {x["id"] for x in p["providers"]} == {"anthropic", "groq", "openai"}
    assert SECRET not in json.dumps(p) and "enc" not in json.dumps(p).lower().replace("encrypted", "")


def test_client_supplied_user_id_cannot_touch_another_users_keys(keys):
    fake, state = keys
    fresh(fake)
    call("POST", None, token="other", body={"action": "dev_key_save", "provider": "groq", "key": SECRET, "user_id": "6", "uid": "6"})
    assert list(state["rows"]) == [("7777777777", "groq")]


def test_save_is_rate_limited(keys):
    fake, state = keys
    fresh(fake)
    codes = [save()[0] for _ in range(8)]
    assert codes[:6] == [200] * 6 and codes[6] == 429


# ---------- no key anywhere ----------
def test_key_never_appears_in_logs_responses_or_errors(keys, caplog):
    fake, state = keys
    fresh(fake)
    caplog.set_level(logging.DEBUG)
    out = [save(), save(key=SECRET + "extra"), call("GET", {"action": "dev_keys"}, token="other"),
           call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": MSGS})]
    state["valid"] = False
    out.append(save())
    state["chat_fail"] = "Groq rejected your key. Replace it in Keys."
    out.append(call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": MSGS}))
    assert SECRET not in json.dumps([o[1] for o in out], default=str)
    assert SECRET not in caplog.text


def test_no_route_or_db_list_method_can_return_ciphertext():
    src = inspect.getsource(importlib.import_module("database").Database.dev_connection_list)
    assert "key_encrypted" not in src                      # the list query never selects the secret
    assert "secret" in inspect.getsource(importlib.import_module("database").Database.dev_connection_secret).lower()
    dev_src = Path(dash_dev.__file__).read_text()
    # exactly two server-side callers, each decrypting only to call the provider: the own-key chat and the GitHub token reader (C4)
    assert dev_src.count("dev_connection_secret") == 2
    for fn in (dash_dev._chat_with_own_key, dash_dev._gh_secret):
        assert "dev_connection_secret" in inspect.getsource(fn)


def test_every_new_dev_handler_starts_with_the_gate():
    for fn in (dash_dev.dev_keys_list, dash_dev.dev_key_save, dash_dev.dev_key_remove):
        body = inspect.getsource(fn).split('"""')[-1] if '"""' in inspect.getsource(fn) else inspect.getsource(fn)
        assert "require_dev(uid, db)" in body.split("\n\n")[0] or "require_dev(uid, db)" in body[:200]


# ---------- chat with your own key ----------
def test_own_key_chat_is_server_to_provider_and_not_counted(keys):
    fake, state = keys
    fresh(fake)
    save()
    st, p, _ = call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": MSGS})
    assert st == 200 and p["reply"] == "own answer" and p["own_key"] is True
    assert state["chat"] == [("groq", SECRET)] and state["ai_spent"] == 0          # weekly 50 untouched


def test_own_key_chat_without_a_saved_key_is_409_and_unknown_model_422(keys):
    st, p, _ = call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": MSGS})
    assert st == 409 and p["code"] == "no_key"
    assert call("POST", None, token="other", body={"action": "dev_chat", "model": "gpt-9", "messages": MSGS})[0] == 422


def test_own_key_chat_is_gated_and_validates_the_conversation(keys):
    fake, state = keys
    assert call("POST", None, token="sid", body={"action": "dev_chat", "model": "groq", "messages": MSGS})[0] == 402
    fresh(fake)
    save()
    assert call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": []})[0] == 422


def test_provider_failure_is_a_safe_502(keys):
    fake, state = keys
    fresh(fake)
    save()
    state["chat_fail"] = "Groq rejected your key. Replace it in Keys."
    st, p, _ = call("POST", None, token="other", body={"action": "dev_chat", "model": "groq", "messages": MSGS})
    assert st == 502 and "Replace it in Keys" in p["message"]


def test_usage_lists_only_connected_models(keys):
    fake, state = keys
    fresh(fake)

    async def status(db, uid, source):
        return {"used": 0, "resets_at": "2026-10-12T00:00:00+00:00"}
    import modules.ai_usage as au
    au_status = au.status
    au.status = status
    try:
        assert [m["id"] for m in call("GET", {"action": "dev_usage"}, token="other")[1]["models"]] == ["default"]
        save(provider="openai")
        assert [m["id"] for m in call("GET", {"action": "dev_usage"}, token="other")[1]["models"]] == ["default", "openai"]
    finally:
        au.status = au_status


# ---------- pure helpers ----------
def test_clean_provider_and_key():
    assert dev_keys.clean_provider(" Groq ") == "groq" and dev_keys.clean_provider("nope") is None
    assert dev_keys.clean_key("a" * 19)[1] and dev_keys.clean_key("a" * 20)[1] is None
    assert dev_keys.last4("abcdefghij") == "ghij"
    assert dev_keys.MAX_CONNECTIONS == 3


def test_payloads_and_text_extraction_per_provider():
    a = dev_keys.build_payload("anthropic", "sys", MSGS)
    assert a["system"] == "sys" and a["messages"] == MSGS and "max_tokens" in a
    o = dev_keys.build_payload("openai", "sys", MSGS)
    assert o["messages"][0] == {"role": "system", "content": "sys"}
    assert dev_keys.extract_text("anthropic", {"content": [{"type": "text", "text": "hi "}]}) == "hi"
    assert dev_keys.extract_text("groq", {"choices": [{"message": {"content": " yo "}}]}) == "yo"
    assert dev_keys.extract_text("groq", {}) == ""


def test_public_view_whitelists_fields():
    v = dev_keys.public_view({"provider": "groq", "last4": "abcd", "key_encrypted": "CIPHER", "created_at": None})
    assert "CIPHER" not in json.dumps(v) and set(v) == {"provider", "label", "last4", "added_at", "updated_at"}


def test_purge_sql_keeps_30_days_after_the_plan_ends():
    src = inspect.getsource(importlib.import_module("database").Database.dev_connections_purge_lapsed)
    assert "dev_monthly" in src and "dev_yearly" in src and "INTERVAL '1 day'" in src
    assert dev_keys.GRACE_DAYS == 30


def test_front_end_has_no_innerhtml_and_keys_input_is_a_password_field():
    js = Path("dashboard/assets/dash.js").read_text()
    assert ".innerHTML" not in js
    assert 'type: "password", autocomplete: "off"' in js
