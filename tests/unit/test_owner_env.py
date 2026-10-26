"""Shared settings saved from the owner's DM form: registry limits, validation, encryption, boot-time loading, the
form's views and the owner gate. No real database or Discord connection is used."""
import asyncio
import os
import types

import pytest

from cryptography.fernet import Fernet

from modules import owner_env as oe


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(oe, "_FERNET", None)


# ── registry ────────────────────────────────────────────────────────────

def test_registry_is_sane():
    names = [s.name for s in oe.SPECS]
    assert len(names) == len(set(names))
    assert not (set(names) & oe.LOCKED)                       # DB url, encryption key, bot tokens, admin id stay on Railway
    assert len(oe.GROUPS) <= 25                               # one select menu
    for g in oe.GROUPS:
        assert 1 <= len(oe.pages(g)) <= 5
        assert all(len(p) <= 5 for p in oe.pages(g))          # a modal holds 5 fields
    assert all(len(s.label) <= 45 for s in oe.SPECS)          # modal label limit
    for must_stay in ("DATABASE_URL", "ENCRYPTION_KEY", "DISCORD_BOT_TOKEN", "ADMIN_ID"):
        assert must_stay not in oe.BY_NAME


def test_every_key_exists_in_config_or_is_read_somewhere():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    text = "".join(p.read_text(errors="ignore") for p in root.glob("*.py")) + \
           "".join(p.read_text(errors="ignore") for d in ("modules", "discord_bot", "api", "utils") for p in (root / d).rglob("*.py"))
    missing = [s.name for s in oe.SPECS if f'"{s.name}"' not in text and f"'{s.name}'" not in text
               and not text.count(s.name)]
    assert not missing, f"managed but never read: {missing}"


# ── validation ──────────────────────────────────────────────────────────

def test_clean_rules():
    url, num, flt, flag, txt = (oe.BY_NAME[n] for n in
                                ("WEBSITE_URL", "DISCORD_SUPPORT_SERVER_ID", "ANIME_PLUS_FEE_USD",
                                 "BOT_PROFILE_VOTE_GATE", "GROQ_API_KEY"))
    assert oe.clean(url, ' "https://x.dev/" ') == (True, "https://x.dev")
    assert not oe.clean(url, "x.dev")[0] and not oe.clean(url, "http://x.dev")[0]
    assert oe.clean(num, "123") == (True, "123") and not oe.clean(num, "12a")[0]
    assert oe.clean(flt, "2.5") == (True, "2.5") and not oe.clean(flt, "abc")[0]
    assert oe.clean(flag, "Off") == (True, "0") and oe.clean(flag, "yes") == (True, "1") and not oe.clean(flag, "2")[0]
    assert not oe.clean(txt, "a\nb")[0] and not oe.clean(txt, "")[0] and not oe.clean(txt, "x" * 500)[0]


def test_secrets_are_encrypted_and_never_shown():
    sec, plain = oe.BY_NAME["GROQ_API_KEY"], oe.BY_NAME["TOPGG_BOT_ID"]
    stored = oe.pack(sec, "gsk_secret_value")
    assert stored.startswith(oe.ENC) and "gsk_secret_value" not in stored
    assert oe.unpack(sec, stored) == "gsk_secret_value"
    assert oe.pack(plain, "123") == "123" and oe.unpack(plain, "123") == "123"
    assert "secret" not in oe.shown(sec, "gsk_secret_value") and oe.shown(sec, "gsk_secret_value") == "set (hidden)"
    assert oe.shown(plain, "123") == "123" and oe.shown(plain, None) == "empty"


# ── boot-time loading ───────────────────────────────────────────────────

def _boot(monkeypatch, rows=None, boom=False):
    monkeypatch.setattr(oe, "_done", False)
    monkeypatch.setattr(oe, "APPLIED", {})
    monkeypatch.setattr(oe, "RAILWAY_HAD", set())
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")

    async def fake(_url):
        if boom:
            raise OSError("db down")
        return rows or []
    monkeypatch.setattr(oe, "_fetch_rows", fake)
    return oe.bootstrap()


def test_bootstrap_db_wins_and_railway_is_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "railway")
    monkeypatch.setenv("TOPGG_BOT_ID", "railway-id")
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    rows = [("env:GROQ_API_KEY", oe.pack(oe.BY_NAME["GROQ_API_KEY"], "from-db")),
            ("env:WEBSITE_URL", "https://site.example")]
    assert _boot(monkeypatch, rows) == 2
    assert os.environ["GROQ_API_KEY"] == "from-db" and os.environ["WEBSITE_URL"] == "https://site.example"
    assert os.environ["TOPGG_BOT_ID"] == "railway-id"          # nothing saved: Railway value untouched
    assert "GROQ_API_KEY" in oe.RAILWAY_HAD and "WEBSITE_URL" not in oe.RAILWAY_HAD


def test_bootstrap_never_touches_locked_or_unknown_keys(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://real")
    rows = [("env:DATABASE_URL", "postgresql://evil"), ("env:ENCRYPTION_KEY", "x"), ("env:NOT_A_KEY", "1")]
    _boot(monkeypatch, rows)
    monkeypatch.setenv("DATABASE_URL", "postgresql://real")
    assert os.environ["DATABASE_URL"] == "postgresql://real" and "NOT_A_KEY" not in os.environ


def test_bootstrap_survives_database_failure_and_bad_ciphertext(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "railway")
    assert _boot(monkeypatch, boom=True) == 0 and os.environ["GROQ_API_KEY"] == "railway"
    assert _boot(monkeypatch, [("env:GROQ_API_KEY", "enc:not-valid")]) == 0 and os.environ["GROQ_API_KEY"] == "railway"


def test_bootstrap_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("OWNER_ENV_DISABLE", "1")
    assert _boot(monkeypatch, [("env:WEBSITE_URL", "https://x.y")]) == 0


def test_status_text(monkeypatch):
    monkeypatch.setattr(oe, "APPLIED", {})
    monkeypatch.setattr(oe, "RAILWAY_HAD", set())
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    assert oe.status("WEBSITE_URL", {}) == ("empty", False)
    assert oe.status("WEBSITE_URL", {"WEBSITE_URL": "https://a.b"})[1] is True      # saved, not running yet
    monkeypatch.setenv("WEBSITE_URL", "https://a.b")
    assert oe.status("WEBSITE_URL", {"WEBSITE_URL": "https://a.b"})[1] is False     # running what is saved
    text, _ = oe.status("GROQ_API_KEY", {"GROQ_API_KEY": "k"})
    assert "k" not in text.replace("hidden", "").replace("Railway", "").replace("saved", "").replace("set", "")


# ── the form ────────────────────────────────────────────────────────────

cog = pytest.importorskip("discord_bot.cogs.owner_env")


def _run(coro):
    return asyncio.run(coro)


def test_views_build_for_every_group():
    async def go():
        saved = {"GROQ_API_KEY": "k", "WEBSITE_URL": "https://a.b"}
        cog.build_home(saved)
        for g in oe.GROUPS:
            v = cog.build_group(g, saved, notice="Saved 1.")
            assert v is not None
    _run(go())


def test_owner_gate(monkeypatch):
    monkeypatch.setattr(cog, "ADMIN_ID", 111)
    monkeypatch.setattr(cog, "DISCORD_CLONE_ADMIN_IDS", {222})
    assert cog._owner_ids() == {111, 222}
    monkeypatch.setattr(cog, "ADMIN_ID", 0)
    assert 0 not in cog._owner_ids()


class _Resp:
    def __init__(self): self.done = False; self.sent = []
    def is_done(self): return self.done
    async def defer(self, **k): self.done = True
    async def send_message(self, *a, **k): self.done = True; self.sent.append((a, k))


class _Inter:
    def __init__(self, uid, clone_id=None):
        self.user = types.SimpleNamespace(id=uid)
        self.client = types.SimpleNamespace(clone_id=clone_id)
        self.response = _Resp()
        self.edits = []
        self.followups = []
        self.followup = types.SimpleNamespace(send=self._fu)
    async def _fu(self, *a, **k): self.followups.append((a, k))
    async def edit_original_response(self, **k): self.edits.append(k)


def test_modal_saves_resets_skips_and_reports_problems(monkeypatch):
    async def go():
        db = {"TOPGG_BOT_ID": "old-id"}
        calls = {"save": [], "reset": []}

        async def load_all(): return dict(db)
        async def save(n, v): calls["save"].append((n, v)); db[n] = v
        async def reset(n): calls["reset"].append(n); db.pop(n, None)
        monkeypatch.setattr(oe, "load_all", load_all)
        monkeypatch.setattr(oe, "save", save)
        monkeypatch.setattr(oe, "reset", reset)
        monkeypatch.setattr(cog, "ADMIN_ID", 111)
        monkeypatch.setenv("DTHINGS_BOT_ID", "from-railway")
        monkeypatch.delenv("TOPGG_TOKEN", raising=False)

        modal = cog.FillModal("Bot lists", 0, dict(db))
        modal.inputs["TOPGG_TOKEN"]._value = "tok_new"             # secret, new value
        modal.inputs["TOPGG_WEBHOOK_SECRET"]._value = ""           # blank: keep
        modal.inputs["TOPGG_BOT_ID"]._value = "-"                  # reset the saved one
        modal.inputs["DTHINGS_API_KEY"]._value = "bad\nvalue"      # rejected
        modal.inputs["DTHINGS_BOT_ID"]._value = "from-railway"     # prefilled, unchanged: not copied into the DB
        it = _Inter(111)
        await modal.on_submit(it)
        assert calls["save"] == [("TOPGG_TOKEN", "tok_new")]
        assert calls["reset"] == ["TOPGG_BOT_ID"]
        assert it.edits and it.edits[0].get("view") is not None

        stranger = _Inter(999)
        assert await modal.interaction_check(stranger) is False and stranger.response.sent
    _run(go())


def test_command_is_owner_only_and_main_bot_only(monkeypatch):
    async def go():
        c = cog.OwnerEnvCog(types.SimpleNamespace())
        monkeypatch.setattr(cog, "ADMIN_ID", 111)
        stranger = _Inter(999)
        await c.envsetup(stranger)
        assert "owner" in stranger.response.sent[0][0][0].lower()
        on_clone = _Inter(111, clone_id=3)
        await c.envsetup(on_clone)
        assert "main bot" in on_clone.response.sent[0][0][0]

        sent = {}

        class DM:
            async def send(self, **k): sent.update(k)

        async def create_dm(): return DM()
        async def load_all(): return {}
        monkeypatch.setattr(oe, "load_all", load_all)
        owner = _Inter(111)
        owner.user.create_dm = create_dm
        await c.envsetup(owner)
        assert sent.get("view") is not None and owner.followups[0][0][0] == "Sent to your DMs."
    _run(go())
