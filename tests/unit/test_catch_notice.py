"""Notice embeds: kinds, colours, titles, the key table, and that no refusal is sent as bare text."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import discord_bot.cogs._views_catch_rules as rules_views
from modules import catch_emoji as emoji
from modules import catch_notice as notice
from modules.catch_i18n import text
from modules.catch_setup import CatchSetup
from modules.catch_theme import state_color
from tests.unit.notice_helpers import body
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow

ROOT = Path(__file__).resolve().parents[2]
EN = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
SOURCES = sorted((ROOT / "discord_bot").rglob("*.py"))


def test_each_kind_has_its_own_colour_icon_and_title():
    seen = set()
    for kind, state in (("error", "danger"), ("warning", "warning"), ("info", "info")):
        e = notice.notice_embed(kind, "catch.unavailable", reason="x")
        assert e.colour == state_color(state)
        assert e.title == f"{emoji.mark('ui', kind)} {text(f'notice.{kind}')}"
        assert emoji.mark("ui", kind) != emoji.FALLBACK
        seen.add((e.colour.value, e.title))
    assert len(seen) == 3


def test_body_is_the_unchanged_locale_text_with_values_filled():
    e = notice.notice_embed("warning", "catch.unavailable", reason="shop_disabled")
    assert e.description == text("catch.unavailable", reason="shop_disabled")
    assert "shop_disabled" in e.description


def test_unknown_kind_is_shown_as_info_and_never_raises():
    e = notice.notice_embed("nonsense", "hub.error")
    assert e.colour == state_color("info") and e.title.endswith(text("notice.info"))


def test_unknown_key_is_shown_as_info_with_the_key_text():
    out = notice.notice_for("totally.unknown")
    assert out["embed"].colour == state_color("info") and "totally.unknown" in out["embed"].description


def test_notice_for_returns_only_an_embed():
    out = notice.notice_for("hub.error")
    assert list(out) == ["embed"] and isinstance(out["embed"], discord.Embed)


def test_long_text_is_capped_for_discord():
    e = notice.notice_embed("info", "catch.unavailable", reason="x" * 9000)
    assert len(e.description) <= 4096


def test_kinds_by_key():
    assert notice.kind_of("hub.error") == "error" and notice.kind_of("catch.sell.load_error") == "error"
    assert notice.kind_of("catch.unavailable") == "warning" and notice.kind_of("ui.not_yours") == "warning"
    assert notice.kind_of("encounter.cooldown") == "info" and notice.kind_of("creature.busy") == "info"
    assert notice.kind_of("evolve.refused_final_form") == "warning"
    assert notice.kind_of("release.refused_locked") == "warning"
    assert notice.kind_of("creature.nick_link") == "warning"
    assert notice.kind_of("something.new") == "info"


def test_every_listed_key_exists_in_the_locale_and_in_one_kind_only():
    for key in notice.KIND_FOR:
        assert f"catch.{key}" in EN, key
    groups = (notice._ERRORS, notice._WARNINGS, notice._INFOS)
    flat = [k for g in groups for k in g]
    assert len(flat) == len(set(flat))


def test_titles_exist_for_every_kind():
    for kind in notice.KINDS:
        assert EN[f"catch.notice.{kind}"].strip()


def test_every_error_key_in_the_locale_is_listed_as_an_error():
    errors = {k[6:] for k in EN if re.search(r"\.(error|load_error)$", k) and k.startswith("catch.")}
    errors -= {"setup.error", "notice.error"}  # owner setup panel and the notice title are not player errors
    assert errors <= set(notice._ERRORS), errors - set(notice._ERRORS)


def test_no_listed_message_is_still_sent_as_bare_text():
    bare = re.compile(r'(?:send|send_message|edit_original_response)\(\s*text\(\s*"((?:catch\.)?[a-z_.]+)"')
    leftovers = []
    for path in SOURCES:
        for m in bare.finditer(path.read_text(encoding="utf-8")):
            key = m.group(1)
            if (key[6:] if key.startswith("catch.") else key) in notice.KIND_FOR:
                leftovers.append((path.name, key))
    assert leftovers == []


def test_every_literal_notice_key_exists_and_has_a_kind():
    pat = re.compile(r'notice_for\(\s*"((?:catch\.)?[a-z_.]+)"')
    keys = {m.group(1) for p in SOURCES for m in pat.finditer(p.read_text(encoding="utf-8"))}
    assert len(keys) > 20
    for key in keys:
        bare = key[6:] if key.startswith("catch.") else key
        assert f"catch.{bare}" in EN, key
        assert bare in notice.KIND_FOR, key


def run_rules(monkeypatch, *, allowed, load_raises=False):
    import asyncio

    rec = Recorder()
    monkeypatch.setattr(rules_views, "check_player_allowed",
                        slow(rec, "gate", SimpleNamespace(allowed=allowed, reason="game_disabled")))

    async def load(*a):
        if load_raises:
            raise RuntimeError("db")
        return CatchSetup()

    monkeypatch.setattr(rules_views, "load_setup", load)
    inter = make_interaction(rec)
    inter.guild_id = 1
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(rules_views.open_rules(inter))
    return sent


def test_a_real_refusal_is_an_ephemeral_warning_notice(monkeypatch):
    (args, kwargs), = run_rules(monkeypatch, allowed=False)
    assert args == () and kwargs["ephemeral"] is True
    assert kwargs["embed"].colour == state_color("warning")
    assert kwargs["embed"].title.endswith(text("notice.warning"))
    assert body(((), kwargs)) == text("catch.unavailable", reason="game_disabled")


def test_a_real_load_failure_is_an_ephemeral_error_notice(monkeypatch):
    (args, kwargs), = run_rules(monkeypatch, allowed=True, load_raises=True)
    assert args == () and kwargs["ephemeral"] is True
    assert kwargs["embed"].colour == state_color("danger")
    assert body(((), kwargs)) == text("rules.error")


def test_server_only_is_an_info_notice(monkeypatch):
    import asyncio

    rec = Recorder()
    inter = make_interaction(rec)
    inter.guild_id = None
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(rules_views.open_rules(inter))
    assert sent[0][1]["embed"].colour == state_color("info")
    assert body(sent[0]) == text("encounter.server_only")


@pytest.mark.parametrize("kind", ["error", "warning", "info"])
def test_titles_fall_back_to_english_in_other_locales(kind):
    assert text(f"notice.{kind}", locale="fr")
