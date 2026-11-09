"""Clones DM the owner which Railway variables a separately hosted copy needs (nothing already saved in the database)."""
import asyncio
import types

import pytest

from modules import clone_hosting as ch

CODE = {"DATABASE_URL", "ENCRYPTION_KEY", "GROQ_API_KEY", "PAYSTACK_SECRET_KEY", "DB_POOL_MAX", "DISCORD_BOT_TOKEN",
        "SINOBANED2_BOT_TOKEN", "PORT", "SHLVL", "PREMIUM_TRIAL_ENABLED"}


def test_required_always_listed_and_saved_names_never_repeated():
    env = {"DATABASE_URL": "x", "ENCRYPTION_KEY": "y", "GROQ_API_KEY": "k", "PAYSTACK_SECRET_KEY": "s"}
    req, extra = ch.needed_variables(env, saved=["PAYSTACK_SECRET_KEY"], read_by_code=CODE)
    assert req == ["DATABASE_URL", "ENCRYPTION_KEY"]
    assert extra == ["GROQ_API_KEY"]                       # the saved one is not repeated


def test_required_listed_even_if_missing_from_environment():
    req, extra = ch.needed_variables({}, [], CODE)
    assert req == ["DATABASE_URL", "ENCRYPTION_KEY"] and extra == []


def test_noise_main_tokens_and_service_specific_vars_are_left_out():
    env = {"DISCORD_BOT_TOKEN": "t", "SINOBANED2_BOT_TOKEN": "t", "PORT": "1", "SHLVL": "1", "RAILWAY_SERVICE_NAME": "svc",
           "UNRELATED_HOST_VAR": "1", "DB_POOL_MAX": "5", "EMPTY_ONE": ""}
    _, extra = ch.needed_variables(env, [], CODE | {"EMPTY_ONE"})
    assert extra == ["DB_POOL_MAX"]


def test_message_holds_no_secret_values_and_uses_reference_variables():
    parts = ch.build_messages(7, "Honeypot", ["DATABASE_URL", "ENCRYPTION_KEY"], ["GROQ_API_KEY"], 12, "bot")
    text = "\n".join(parts)
    assert "--clone-id 7" in text and "CLONE_MANAGER_EXCLUDE_IDS" in text and "12 other setting" in text
    assert "GROQ_API_KEY=${{bot.GROQ_API_KEY}}" in text and "DATABASE_URL=${{bot.DATABASE_URL}}" in text


def test_without_a_known_service_lines_are_bare_names():
    parts = ch.build_messages(1, "x", ["DATABASE_URL"], [], 0, None)
    assert "DATABASE_URL=\n" in "\n".join(parts) and "${{" not in "\n".join(parts)


def test_long_lists_are_split_under_the_discord_limit():
    extra = [f"SOME_LONG_VARIABLE_NAME_{i:03d}" for i in range(200)]
    parts = ch.build_messages(1, "x", ["DATABASE_URL", "ENCRYPTION_KEY"], extra, 0, "main-service")
    assert len(parts) > 2 and all(len(p) <= 2000 for p in parts)
    joined = "\n".join(parts)
    assert all(f"{n}=${{{{main-service.{n}}}}}" in joined for n in extra)


def test_signature_changes_only_when_the_list_does():
    a = ch.signature(["A"], ["B"], "s")
    assert a == ch.signature(["A"], ["B"], "s")
    assert a != ch.signature(["A"], ["B", "C"], "s") and a != ch.signature(["A"], ["B"], "other")


def test_scan_finds_names_the_code_reads():
    names = ch.names_read_by_code()
    assert {"DATABASE_URL", "ENCRYPTION_KEY", "GROQ_API_KEY", "DB_POOL_MAX"} <= names


# ── sending ──────────────────────────────────────────────────────────────
class _Db:
    def __init__(self):
        self.store = {}

    async def get_global_setting(self, k): return self.store.get(k)
    async def set_global_setting(self, k, v): self.store[k] = v
    async def get_discord_clone(self, cid): return {"bot_username": "Honeypot"}


@pytest.fixture
def send_env(monkeypatch):
    import config
    import database
    import discord_bot.dm_send as dm
    from modules import owner_env
    db = _Db()
    sent = []
    ok = {"own": True, "main": True}

    async def fake_dm(user_id, content, token):
        if not ok["own" if token == "own-token" else "main"]:
            return False
        sent.append((user_id, token, content))
        return True

    async def saved():
        return {"PAYSTACK_SECRET_KEY": "v", "UNREADABLE": None}

    monkeypatch.setattr(database, "db", db)
    monkeypatch.setattr(dm, "dm_user", fake_dm)
    monkeypatch.setattr(owner_env, "load_all", saved)
    monkeypatch.setattr(config, "DISCORD_OWNER_BROADCAST_IDS", {42}, raising=False)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "main-token", raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "bot")
    monkeypatch.delenv("OWNER_ENV_DISABLE", raising=False)
    bot = types.SimpleNamespace(clone_id=7, http=types.SimpleNamespace(token="own-token"), user="Honeypot#0001")
    return bot, db, sent, ok


def run(c):
    return asyncio.run(c)


def test_sent_once_then_skipped_until_the_list_changes(send_env, monkeypatch):
    bot, db, sent, _ = send_env
    assert run(ch.send_hosting_guide(bot, delay=0)) is True
    assert sent and all(uid == 42 and tok == "own-token" for uid, tok, _ in sent)
    assert "clone_hosting_dm:7" in db.store
    n = len(sent)
    assert run(ch.send_hosting_guide(bot, delay=0)) is False and len(sent) == n      # same list: not sent again
    monkeypatch.setenv("GROQ_API_KEY", "k")                                         # list changed -> sent again
    assert run(ch.send_hosting_guide(bot, delay=0)) is True and len(sent) > n


def test_database_saved_names_are_not_in_the_dm(send_env, monkeypatch):
    bot, _, sent, _ = send_env
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "secret-value")
    run(ch.send_hosting_guide(bot, delay=0))
    text = "\n".join(c for *_, c in sent)
    assert "PAYSTACK_SECRET_KEY" not in text and "secret-value" not in text and "DATABASE_URL" in text


def test_falls_back_to_the_main_bot_when_the_clone_cannot_dm(send_env):
    bot, db, sent, ok = send_env
    ok["own"] = False
    assert run(ch.send_hosting_guide(bot, delay=0)) is True
    assert sent and all(tok == "main-token" for _, tok, _ in sent)


def test_nothing_stored_when_nobody_can_dm_so_it_retries_next_start(send_env):
    bot, db, sent, ok = send_env
    ok["own"] = ok["main"] = False
    assert run(ch.send_hosting_guide(bot, delay=0)) is False
    assert not db.store and not sent


def test_main_bot_never_sends_it(send_env):
    bot, _, sent, _ = send_env
    bot.clone_id = None
    assert run(ch.send_hosting_guide(bot, delay=0)) is False and not sent


def test_never_raises(send_env, monkeypatch):
    bot, db, _, _ = send_env

    async def boom(k): raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_global_setting", boom)
    assert run(ch.send_hosting_guide(bot, delay=0)) is False
