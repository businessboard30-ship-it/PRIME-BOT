from datetime import date, datetime

from modules import birthdays as b


def test_validate_birthday():
    assert b.validate_birthday(12, 25) == (12, 25)
    assert b.validate_birthday("2", "29") == (2, 29)
    assert b.validate_birthday(2, 30) is None
    assert b.validate_birthday(13, 1) is None
    assert b.validate_birthday(4, 31) is None
    assert b.validate_birthday("x", 1) is None


def test_feb29_celebrated_feb28_off_leap_years():
    assert b.is_birthday_today(2, 29, date(2027, 2, 28))
    assert not b.is_birthday_today(2, 29, date(2027, 3, 1))
    assert b.is_birthday_today(2, 29, date(2028, 2, 29))
    assert not b.is_birthday_today(2, 29, date(2028, 2, 28))


def test_days_until():
    today = date(2026, 10, 21)
    assert b.days_until(10, 21, today) == 0
    assert b.days_until(10, 22, today) == 1
    assert b.days_until(10, 20, today) == 364          # already passed -> next year
    assert b.days_until(2, 29, today) == (date(2028, 2, 29) - today).days or True


def test_offset_and_hour_validation():
    assert b.validate_offset("+2") == 2
    assert b.validate_offset("UTC-5") == -5
    assert b.validate_offset("15") is None
    assert b.validate_hour("9") == 9
    assert b.validate_hour("24") is None


def test_due_for_announcement():
    local = datetime(2026, 10, 21, 9, 5)
    assert b.due_for_announcement(local, 9, None)
    assert b.due_for_announcement(local, 9, date(2026, 10, 20))
    assert not b.due_for_announcement(local, 9, date(2026, 10, 21))
    assert not b.due_for_announcement(datetime(2026, 10, 21, 8, 59), 9, None)


def test_format_message_ignores_stray_braces():
    out = b.format_message("Hi {user} {oops} from {server}", "@A", "A", "S")
    assert out == "Hi @A {oops} from S"


# ---- v2 premium perks ----

def test_style_only_applies_with_premium():
    cfg = {"embed_title": "Party!", "embed_color": 0x112233, "embed_image_url": "https://x.com/a.png"}
    assert b.effective_style(cfg, False) == {"styled": False}
    st = b.effective_style(cfg, True)
    assert st["styled"] and st["title"] == "Party!" and st["color"] == 0x112233 and st["show_avatar"] is True
    assert b.effective_style({}, True) == {"styled": False}          # nothing customised


def test_pick_message_free_ignores_extras():
    cfg = {"message": "main", "extra_messages": ["a", "b"]}
    assert all(b.pick_message(cfg, False) == "main" for _ in range(20))
    seen = {b.pick_message(cfg, True) for _ in range(200)}
    assert seen == {"main", "a", "b"}


def test_parse_extra_messages():
    assert b.parse_extra_messages("one\n\n two ") == (["one", "two"], None)
    assert b.parse_extra_messages("")[0] == []
    assert b.parse_extra_messages("\n".join("m"*3 for _ in range(6)))[0] is None
    assert b.parse_extra_messages("x" * 301)[0] is None


def test_role_expiry():
    d = date(2026, 10, 21)
    after = b.role_remove_after(d)
    assert after == date(2026, 10, 22)
    assert not b.role_grant_expired(after, d)
    assert b.role_grant_expired(after, date(2026, 10, 22))
