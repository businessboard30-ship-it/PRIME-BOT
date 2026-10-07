"""AI voice replies: request detection, marker handling, speech prep, WAV joining."""
import asyncio
import io
import wave

import pytest

from modules import ai_voice


def run(c):
    return asyncio.run(c)


@pytest.mark.parametrize("text", [
    "send a voice note", "reply with voice", "can you reply in a voice message",
    "say it out loud", "talk to me", "let me hear you", "use your voice",
])
def test_explicit_voice_requests_are_detected(text):
    assert ai_voice.wants_voice(text)


@pytest.mark.parametrize("text", [
    "do you speak english?", "best voice actor for goku", "I love his voice", "voice chat is broken", "hello",
])
def test_ordinary_mentions_of_voice_are_not_requests(text):
    assert not ai_voice.wants_voice(text)


def test_user_can_turn_automatic_voice_off_and_on():
    assert ai_voice.voice_preference_change("stop sending voice notes") == "off"
    assert ai_voice.voice_preference_change("text only please") == "off"
    assert ai_voice.voice_preference_change("you can use voice notes again") == "auto"
    assert ai_voice.voice_preference_change("send a voice note") is None


def test_marker_is_stripped_and_reported():
    assert ai_voice.split_marker("[[VOICE]] hey there") == ("hey there", True)
    assert ai_voice.split_marker("hey [[ voice ]]there") == ("hey there", True)
    assert ai_voice.split_marker("plain") == ("plain", False)


def test_voice_reply_is_a_str_that_carries_the_flag():
    r = ai_voice.VoiceReply("hi")
    assert r == "hi" and r.voice is True and not getattr("hi", "voice", False)


def test_speech_text_drops_markdown_links_emoji_and_discord_tokens():
    out = ai_voice.clean_for_speech("**Hey** <@123> see https://x.com/a `code` 😂🔥 <:wow:99> bye \x00SUPPORT_BTN\x00")
    assert out == "Hey see the link code bye"


def test_chunks_respect_groqs_200_char_limit_and_the_chunk_cap():
    long = " ".join(f"Sentence number {i} goes here." for i in range(60)) + " " + "x" * 500
    chunks = ai_voice.split_chunks(long)
    assert 1 <= len(chunks) <= ai_voice.MAX_CHUNKS
    assert all(len(c) <= ai_voice.CHUNK_CHARS for c in chunks)
    assert ai_voice.split_chunks("") == []
    assert ai_voice.split_chunks("Short one.") == ["Short one."]


def _wav(frames: bytes, rate=24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def test_wav_chunks_are_joined_into_one_file():
    joined = ai_voice.join_wavs([_wav(b"\x01\x00" * 100), _wav(b"\x02\x00" * 50)])
    with wave.open(io.BytesIO(joined), "rb") as r:
        assert r.getnframes() == 150
    assert ai_voice.join_wavs([_wav(b"\x00\x00")]) == _wav(b"\x00\x00")


def test_mismatched_wavs_are_refused_instead_of_corrupted():
    assert ai_voice.join_wavs([_wav(b"\x00\x00" * 10, 24000), _wav(b"\x00\x00" * 10, 48000)]) is None


def test_render_voice_uses_cleaned_text_and_marks_cooldown(monkeypatch):
    seen = {}

    async def fake_synth(text, voice):
        seen["text"], seen["voice"] = text, voice
        return b"RIFFfake"
    monkeypatch.setattr(ai_voice, "synthesize", fake_synth)
    ai_voice._last_voice.clear()
    assert run(ai_voice.render_voice(1, "**hey** 😂 you", "autumn", explicit=False)) == b"RIFFfake"
    assert seen == {"text": "hey you", "voice": "autumn"}
    # AI-chosen voice is rate limited per user; an explicit request is not refused
    assert run(ai_voice.render_voice(1, "hey again", "autumn", explicit=False)) is None
    assert run(ai_voice.render_voice(1, "hey again", "autumn", explicit=True)) == b"RIFFfake"


def test_ai_chosen_voice_that_is_too_long_stays_text(monkeypatch):
    async def fake_synth(text, voice):
        raise AssertionError("must not synthesise")
    monkeypatch.setattr(ai_voice, "synthesize", fake_synth)
    ai_voice._last_voice.clear()
    assert run(ai_voice.render_voice(2, "word " * 400, "autumn", explicit=False)) is None


def test_explicit_voice_for_a_long_answer_speaks_a_trimmed_version(monkeypatch):
    seen = {}

    async def fake_synth(text, voice):
        seen["n"] = len(text)
        return b"RIFFok"
    monkeypatch.setattr(ai_voice, "synthesize", fake_synth)
    ai_voice._last_voice.clear()
    assert run(ai_voice.render_voice(3, "This is a sentence. " * 100, "autumn", explicit=True)) == b"RIFFok"
    assert seen["n"] <= ai_voice.MAX_SPOKEN_CHARS


def test_tts_failure_means_no_audio_so_the_caller_sends_text(monkeypatch):
    async def fake_synth(text, voice):
        return None
    monkeypatch.setattr(ai_voice, "synthesize", fake_synth)
    ai_voice._last_voice.clear()
    assert run(ai_voice.render_voice(4, "hello", "autumn", explicit=True)) is None
    assert not ai_voice.on_cooldown(4)      # a failed render doesn't burn the cooldown
