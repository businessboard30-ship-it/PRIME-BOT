"""AI cog: character/voice changes in chat, voice delivery with safe fallback; verification wording."""
import asyncio
import io
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import ai_tools, _views_verification
from modules import ai_prefs, ai_voice


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def cog(monkeypatch):
    c = ai_tools.AIToolsCog(types.SimpleNamespace(clone_id=None, application_id=1, user=types.SimpleNamespace(id=1)))
    saved = {}

    async def set_char(uid, key):
        saved["character"] = key
        return True

    async def set_mode(uid, mode):
        saved["voice_mode"] = mode
        return True

    async def get_prefs(uid):
        return saved.get("character", "genz"), saved.get("voice_mode", "auto")
    monkeypatch.setattr(ai_prefs, "set_character", set_char)
    monkeypatch.setattr(ai_prefs, "set_voice_mode", set_mode)
    monkeypatch.setattr(ai_prefs, "get_prefs", get_prefs)
    c.saved = saved
    return c


def test_switching_character_in_chat_saves_it_and_replies_in_that_voice(cog):
    text, view = run(cog._prefs_quick_answer("switch to gentle", 1))
    assert cog.saved["character"] == "gentle" and view is None
    assert text == ai_prefs.CHARACTERS["gentle"]["intro"]


def test_change_character_shows_a_picker_only_its_owner_can_use(cog):
    text, view = run(cog._prefs_quick_answer("change your character", 1))
    assert isinstance(view, ai_tools.CharacterPickView)
    options = view.children[0].options
    assert [o.value for o in options] == ["genz", "gentle", "sensei"]
    assert [o.default for o in options] == [True, False, False]
    stranger = MagicMock(); stranger.user.id = 2; stranger.response.send_message = AsyncMock()
    assert run(view.interaction_check(stranger)) is False
    owner = MagicMock(); owner.user.id = 1
    assert run(view.interaction_check(owner)) is True


def test_picker_selection_is_saved(cog):
    _, view = run(cog._prefs_quick_answer("change your character", 1))
    i = MagicMock(); i.user.id = 1; i.data = {"values": ["sensei"]}; i.response.edit_message = AsyncMock()
    run(view._picked(i))
    assert cog.saved["character"] == "sensei"
    assert i.response.edit_message.await_args.kwargs["content"] == ai_prefs.CHARACTERS["sensei"]["intro"]


def test_voice_can_be_turned_off_and_on_in_chat(cog):
    text, _ = run(cog._prefs_quick_answer("no more voice notes", 1))
    assert cog.saved["voice_mode"] == "off" and "no more voice" in text.lower()
    assert run(cog._voice_setting(1, "hello")) is None                       # AI may no longer choose voice
    assert run(cog._voice_setting(1, "send a voice note")) == "forced"       # but an explicit ask still works
    run(cog._prefs_quick_answer("you can use voice notes again", 1))
    assert run(cog._voice_setting(1, "hello")) == "auto"


def test_normal_chat_is_not_treated_as_a_settings_change(cog):
    assert run(cog._prefs_quick_answer("show me anime characters", 1)) is None
    assert run(cog._prefs_quick_answer("send a voice note", 1)) is None      # that's a request, not a setting
    assert cog.saved == {}


def test_voice_file_uses_the_characters_voice(cog, monkeypatch):
    cog.saved["character"] = "gentle"
    seen = {}

    async def fake_render(uid, text, voice, explicit):
        seen.update(voice=voice, explicit=explicit)
        return b"RIFFaudio"
    monkeypatch.setattr(ai_voice, "render_voice", fake_render)
    f = run(cog._voice_file(1, "hi", explicit=True))
    assert isinstance(f, discord.File) and f.filename == "voice.wav"
    assert seen == {"voice": ai_prefs.CHARACTERS["gentle"]["voice"], "explicit": True}


def test_voice_failure_falls_back_to_none_not_an_exception(cog, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("tts down")
    monkeypatch.setattr(ai_voice, "render_voice", boom)
    assert run(cog._voice_file(1, "hi", explicit=False)) is None


def _message(guild=True, content="send me a voice note"):
    m = MagicMock()
    m.author.bot = False
    m.author.id = 10
    m.content = content
    m.guild = MagicMock() if guild else None
    m.guild and setattr(m.guild, "id", 55)
    m.mentions = [cog_user] if guild else []
    m.reference = None
    m.reply = AsyncMock()
    m.channel.send = AsyncMock()
    m.channel.typing = MagicMock(return_value=_Typing())
    return m


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


cog_user = types.SimpleNamespace(id=1)


def _wire_chat(cog, monkeypatch, reply):
    cog.bot.user = cog_user
    async def run_turn(message, content, history):
        return reply, None
    monkeypatch.setattr(cog, "_run_reply_turn", run_turn)
    monkeypatch.setattr(cog, "_roast_active_in", AsyncMock(return_value=False))
    monkeypatch.setattr(ai_tools, "is_reply_chat_enabled", AsyncMock(return_value=True))


def test_voice_reply_is_sent_as_an_audio_attachment_with_the_text(cog, monkeypatch):
    _wire_chat(cog, monkeypatch, ai_voice.VoiceReply("hey, here's a voice note"))
    monkeypatch.setattr(cog, "_voice_file", AsyncMock(return_value=discord.File(io.BytesIO(b"RIFF"), "voice.wav")))
    m = _message(content="<@1> send me a voice note")
    run(cog.on_bot_chat(m))
    kw = m.reply.await_args.kwargs
    assert m.reply.await_args.args[0] == "hey, here's a voice note"       # caption keeps it accessible
    assert kw["file"].filename == "voice.wav"


def test_plain_reply_has_no_attachment_and_never_calls_tts(cog, monkeypatch):
    _wire_chat(cog, monkeypatch, "just text")
    vf = AsyncMock()
    monkeypatch.setattr(cog, "_voice_file", vf)
    m = _message(content="<@1> hello")
    run(cog.on_bot_chat(m))
    assert "file" not in m.reply.await_args.kwargs
    vf.assert_not_called()


def test_tts_failure_still_delivers_the_text(cog, monkeypatch):
    _wire_chat(cog, monkeypatch, ai_voice.VoiceReply("still here"))
    monkeypatch.setattr(cog, "_voice_file", AsyncMock(return_value=None))
    m = _message(content="<@1> voice note please")
    run(cog.on_bot_chat(m))
    assert m.reply.await_args.args[0] == "still here" and "file" not in m.reply.await_args.kwargs


def test_missing_attach_permission_retries_without_the_audio(cog, monkeypatch):
    _wire_chat(cog, monkeypatch, ai_voice.VoiceReply("text survives"))
    monkeypatch.setattr(cog, "_voice_file", AsyncMock(return_value=discord.File(io.BytesIO(b"RIFF"), "voice.wav")))
    m = _message(content="<@1> voice note please")
    sent = []

    async def reply(text, **kw):
        sent.append("file" in kw)
        if "file" in kw:
            raise discord.Forbidden(MagicMock(status=403, reason="no"), "Missing Permissions")
    m.reply = reply
    run(cog.on_bot_chat(m))
    assert sent == [True, False]


def test_dm_voice_reply_goes_through_channel_send(cog, monkeypatch):
    _wire_chat(cog, monkeypatch, ai_voice.VoiceReply("dm voice"))
    monkeypatch.setattr(cog, "_voice_file", AsyncMock(return_value=discord.File(io.BytesIO(b"RIFF"), "voice.wav")))
    monkeypatch.setattr(cog, "_dm_context", AsyncMock(return_value=[]))
    m = _message(guild=False, content="talk to me")
    run(cog.on_bot_chat(m))
    assert m.channel.send.await_args.kwargs["file"].filename == "voice.wav"


# ── verification wording ─────────────────────────────────────────────────

@pytest.mark.parametrize("turnstile", [True, False])
def test_verification_panel_no_longer_promises_a_math_question(monkeypatch, turnstile):
    monkeypatch.setattr(_views_verification._cfg, "TURNSTILE_ENABLED", turnstile, raising=False)
    desc = _views_verification.build_verify_panel_embed("Srv", "captcha").description
    assert "math" not in desc.lower() and "cloudflare" not in desc.lower()
    assert "security check" in desc


def test_button_mode_description_is_unchanged():
    desc = _views_verification.build_verify_panel_embed("Srv", "button").description
    assert "security check" not in desc and "verify you're a real person" in desc
