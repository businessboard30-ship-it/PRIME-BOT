"""Welcome/goodbye placeholders: one pass, no pings, no re-expansion, unknown ones untouched."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from modules import welcome_vars as W

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def fill(t, **kw):
    base = dict(mention="<@5>", name="Sam", guild_name="Hub", count=1204, created_at=NOW - timedelta(days=800), rules_channel_id=99, now=NOW)
    base.update(kw)
    return W.fill(t, **base)


@pytest.mark.parametrize("n,out", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"),
                                   (22, "22nd"), (101, "101st"), (111, "111th"), (1204, "1,204th"), (1000000, "1,000,000th")])
def test_ordinal(n, out):
    assert W.ordinal(n) == out


@pytest.mark.parametrize("days,out", [(0, "0 days"), (1, "1 day"), (30, "30 days"), (59, "59 days"), (60, "2 months"), (364, "12 months"),
                                      (365, "1 year"), (800, "2 years")])
def test_account_age(days, out):
    assert W.account_age(NOW - timedelta(days=days), NOW) == out
    assert W.account_age(None) == "unknown"


def test_all_placeholders():
    out = fill("{member}|{name}|{guild}|{count}|{count_ordinal}|{account_age}|{date}|{rules}|{channel:123456789012345678}")
    assert out == "<@5>|Sam|Hub|1204|1,204th|2 years|9 Oct 2026|<#99>|<#123456789012345678>"


def test_unknown_and_malformed_placeholders_are_left_alone():
    t = "{unknown} {count_} {channel:12} {channel:abc} {{count}} {"
    assert fill(t) == t.replace("{{count}}", "{1204}")


def test_rules_fallback_without_a_rules_channel():
    assert fill("{rules}", rules_channel_id=None) == "the rules channel"


def test_single_pass_a_name_cannot_expand_another_placeholder():
    out = fill("{name} is {count}", name="{count}")
    assert out.endswith(" is 1204") and out.startswith("{\u200bcount}") and "1204 is" not in out


def test_names_cannot_ping():
    for bad in ("@everyone", "@here", "<@&123>", "x@everyone"):
        out = fill("Hi {name} to {guild}", name=bad, guild_name=bad)
        assert "@everyone" not in out and "@here" not in out
    assert fill("{member}", mention="<@5>") == "<@5>"           # the real mention is intentional


def test_long_names_are_bounded():
    assert len(fill("{name}", name="x" * 5000)) == W.NAME_MAX


def test_fill_for_member_uses_guild_and_rules_channel():
    m = MagicMock()
    m.mention, m.display_name, m.created_at = "<@5>", "Sam", NOW - timedelta(days=10)
    m.guild.name, m.guild.member_count, m.guild.rules_channel.id = "Hub", 3, 77
    assert W.fill_for_member("{name} #{count_ordinal} {rules} {account_age}", m, now=NOW) == "Sam #3rd <#77> 10 days"


def test_cogs_use_the_shared_implementation():
    from pathlib import Path
    for f in ("discord_bot/cogs/welcome.py", "discord_bot/cogs/_views_welcome.py"):
        t = Path(f).read_text()
        assert "welcome_vars.fill_for_member" in t and '.replace("{count}"' not in t, f
    assert "welcome_vars.fill(" in Path("discord_bot/cogs/welcome_extras.py").read_text()
    import re
    desc = re.search(r'name="message", description="([^"]+)"', Path("discord_bot/cogs/welcome.py").read_text()).group(1)
    assert len(desc) <= 100                                  # Discord's slash-command description limit
