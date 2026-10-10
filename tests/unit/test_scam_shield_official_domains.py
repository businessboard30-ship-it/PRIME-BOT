"""Discord's own domains are never flagged by a domain rule; look-alikes still are."""
from modules import scam_shield as ss


def _rules(*domains):
    ss._c.domains = [(i + 1, d) for i, d in enumerate(domains)]
    ss._c.words = []


def test_official_discord_links_are_not_flagged_even_with_a_rule_on_them():
    _rules("discord.com", "discordapp.com")
    for t in ("see https://discord.com/channels/1/2/3", "https://cdn.discordapp.com/attachments/1/2/a.png",
              "discord.com/developers", "https://canary.discord.com/app"):
        assert ss.match_text(t) is None, t


def test_lookalikes_and_userinfo_tricks_still_match():
    _rules("discord.com", "evil.xyz")
    assert ss.match_text("https://discord.com.evil.xyz/login") == ("domain", "evil.xyz", 2)
    assert ss.match_text("https://discord.com@evil.xyz/x") is not None


def test_other_rules_and_invites_are_unaffected():
    _rules("fatowin.com", "discord.gg")
    assert ss.match_text("join https://fatowin.com/bonus")[0] == "domain"
    assert ss.match_text("https://discord.gg/abc")[0] == "domain"   # invites are not exempted
