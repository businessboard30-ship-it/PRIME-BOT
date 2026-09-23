from modules.ad_links import split_ad_links


def test_links_become_buttons_and_leave_text():
    (title, desc), buttons = split_ad_links(
        ["Cool bots", "Join https://discord.gg/abc123, see https://example.com/shop"],
        target_url="https://t.me/mychan",
    )
    assert "http" not in title + desc
    assert [b[0] for b in buttons] == ["Open Telegram", "Join Server", "Visit example.com"]


def test_na_target_and_dedupe():
    _, buttons = split_ad_links(["a https://a.com https://a.com"], target_url="N/A")
    assert buttons == [("Visit a.com", "https://a.com")]


def test_extra_links_are_wrapped_not_expanded():
    text = " ".join(f"https://s{i}.com" for i in range(7))
    (out,), buttons = split_ad_links([text])
    assert len(buttons) == 5
    assert "<https://s5.com>" in out and "<https://s6.com>" in out
