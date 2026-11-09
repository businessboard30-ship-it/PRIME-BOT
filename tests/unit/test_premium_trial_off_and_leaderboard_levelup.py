"""Premium trial is off by default and can't be started from an old screen; the daily XP leaderboard only posts after a level-up."""
import asyncio
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_trial_default_is_off():
    src = (ROOT / "config.py").read_text()
    assert 'os.getenv("PREMIUM_TRIAL_ENABLED", "0")' in src


def test_stale_start_trial_button_is_refused_when_trials_are_off(monkeypatch):
    pytest.importorskip("discord")
    import config
    from discord_bot.cogs import _views_premium_v2 as v
    monkeypatch.setattr(config, "PREMIUM_TRIAL_ENABLED", False)
    calls = []

    async def send(msg, **kw): calls.append(msg)
    inter = types.SimpleNamespace(response=types.SimpleNamespace(send_message=send),
                                  user=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_guild=True, administrator=True)))

    async def boom(*a, **k): raise AssertionError("a trial must not be started")
    monkeypatch.setattr(v.db, "start_guild_premium_trial", boom)
    view = v.PremiumWizardView.__new__(v.PremiumWizardView)
    asyncio.run(view._start_trial(inter))
    assert calls and "no longer available" in calls[0]


def test_due_query_requires_a_level_up_since_the_last_post():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def get_due_leaderboard_autoposts"):src.index("async def mark_leaderboard_levelup")]
    assert "leaderboard_last_levelup_at IS NOT NULL" in body
    assert "leaderboard_last_levelup_at > leaderboard_last_posted_at" in body
    assert "ADD COLUMN IF NOT EXISTS leaderboard_last_levelup_at TIMESTAMPTZ" in src


def test_levelup_stamp_never_breaks_a_level_up():
    pytest.importorskip("discord")
    from discord_bot.cogs import leveling

    class Boom:
        async def mark_leaderboard_levelup(self, g, c): raise RuntimeError("db down")
    cog = leveling.LevelingCog.__new__(leveling.LevelingCog)
    old = leveling.db
    leveling.db = Boom()
    try:
        asyncio.run(cog._stamp_levelup(1, None))      # must not raise
    finally:
        leveling.db = old


def test_levelup_is_stamped_in_text_and_voice_paths():
    for rel in ("discord_bot/cogs/leveling.py", "discord_bot/cogs/voice_xp.py"):
        assert "_stamp_levelup(" in (ROOT / rel).read_text()


def test_vision_model_default_is_the_current_groq_one():
    line = next(l for l in (ROOT / "modules/ai_features.py").read_text().splitlines() if l.startswith("AI_VISION_MODEL ="))
    default = line.split("or ", 1)[1].split("#")[0].strip()
    assert default == '"qwen/qwen3.8-27b"'          # qwen3.6-27b and llama-4-scout no longer exist on Groq
