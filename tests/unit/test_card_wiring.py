# path: tests/unit/test_card_wiring.py
"""The economy pieces only matter if they're actually connected. These guard the connections."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _src(rel):
    return (ROOT / rel).read_text()


def test_level_up_pays_coins_and_cannot_break_the_level_up():
    s = _src("discord_bot/cogs/leveling.py")
    i = s.index("card_rewards.award_levelup(message.author.id, clone_id, old_level, new_level)")
    block = s[s.rindex("try:", 0, i):s.index("# Clan flavor card", i)]
    assert "except Exception" in block                      # a reward failure is logged, never raised
    assert s.index("if new_level > old_level and isinstance") < i


def test_sweeper_cog_is_loaded_main_bot_only_and_adds_no_commands():
    assert 'discord_bot.cogs.card_hub' in _src("discord_bot/bot.py")
    s = _src("discord_bot/cogs/card_hub.py")
    assert "clone_id\", None) is None" in s                 # clones don't repeat the database-global sweep
    assert "app_commands" not in s and "@commands.command" not in s


def test_migration_093_is_wired_and_additive():
    db = _src("database.py")
    assert '"093_card_levelup_rewards.sql"' in db
    sql = _src("database/migrations/093_card_levelup_rewards.sql").upper()
    for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert bad not in sql
