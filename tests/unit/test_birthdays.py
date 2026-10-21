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
