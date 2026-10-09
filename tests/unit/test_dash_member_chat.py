"""Card plan free chats (B4): 402 without the plan, exactly 10 a week, refund on failure, kill switch, counters
separate from Developer's 50, week boundary, and no Discord code path spends the allowance."""
import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from api import dash
from modules import ai_usage, dev_chat
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  ("sid" = user 6, card plan only; "other" = dev_monthly)

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def chat(mem, monkeypatch):
    fake, _ = mem
    state = {"used": {}, "asked": [], "fail": False, "switches": set(), "systems": []}

    async def consume(uid, ws, src, limit):
        k = (uid, ws, src)
        if state["used"].get(k, 0) >= limit:
            return None
        state["used"][k] = state["used"].get(k, 0) + 1
        return state["used"][k]

    async def get(uid, ws, src):
        return state["used"].get((uid, ws, src), 0)

    async def refund(uid, ws, src):
        k = (uid, ws, src)
        state["used"][k] = max(state["used"].get(k, 0) - 1, 0)

    async def ask(messages, system=None):
        state["asked"].append(messages)
        state["systems"].append(system)
        if state["fail"]:
            raise RuntimeError("The AI service is busy. Try again in a moment.")
        return "hi there"

    async def switches():
        return set(state["switches"])
    for n, f in (("ai_usage_get", get), ("ai_usage_consume", consume), ("ai_usage_refund", refund)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(dev_chat, "ask", ask)
    monkeypatch.setattr(importlib.import_module("modules.admin_controls"), "current_switches", switches)

    async def pay_mode(_=None):
        return "split"
    monkeypatch.setattr(fake, "get_payment_mode", pay_mode, raising=False)
    dash._owner_hits.clear()
    return state


def send(token="sid", messages=MSGS):
    return call("POST", None, token=token, body={"action": "member_chat", "messages": messages})


def test_card_plan_limit_is_exactly_ten_and_dev_is_fifty():
    assert ai_usage.LIMITS["card_plan"] == 10 and ai_usage.LIMITS["dev"] == 50


def test_chat_needs_the_card_plan(chat):
    st, p, _ = send(token="other")                       # dev_monthly only: no card plan
    assert st == 402 and p["code"] == "subscription_required" and p["plans_path"] == "#/me/plans"
    assert chat["asked"] == [] and chat["used"] == {}


def test_ten_chats_then_refused_with_reset_time(chat):
    for i in range(10):
        dash._owner_hits.clear()                 # the per-minute route limit is a different guard; this test is the weekly one
        st, p, _ = send()
        assert st == 200 and p["reply"] == "hi there" and p["used"] == i + 1 and p["limit"] == 10
    dash._owner_hits.clear()
    st, p, _ = send()
    assert st == 429 and p["code"] == "weekly_limit" and "reset" in p["message"] and len(chat["asked"]) == 10


def test_failed_model_call_is_refunded(chat):
    chat["fail"] = True
    st, _, _ = send()
    assert st == 502 and sum(chat["used"].values()) == 0


def test_kill_switch_stops_before_spending(chat):
    chat["switches"] = {"ai"}
    st, _, _ = send()
    assert st == 503 and chat["asked"] == [] and chat["used"] == {}


def test_bad_conversation_does_not_spend(chat):
    assert send(messages=[{"role": "system", "content": "x"}])[0] == 422
    assert send(messages=[])[0] == 422
    assert chat["used"] == {} and chat["asked"] == []


def test_counter_is_the_session_users_and_the_card_source(chat):
    send()
    ((uid, ws, src),) = chat["used"].keys()
    assert uid == "6" and src == "card_plan" and ws == ai_usage.week_start()


def test_uses_the_member_prompt_and_client_cannot_pick_it(chat):
    call("POST", None, body={"action": "member_chat", "messages": MSGS, "system": "evil", "model": "x", "user_id": "7777777777"})
    assert chat["systems"] == [dev_chat.MEMBER_SYSTEM_PROMPT] and chat["asked"][0] == MSGS
    assert list(chat["used"])[0][0] == "6"


def test_week_resets_on_monday_utc():
    sun = datetime(2026, 10, 11, 23, 59, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert ai_usage.week_start(sun) != ai_usage.week_start(mon)
    assert ai_usage.resets_at(sun) == datetime(2026, 10, 12, tzinfo=timezone.utc)


def test_no_discord_code_spends_the_allowance():
    root = Path(dash.__file__).resolve().parent.parent
    allowed = {"ai_usage.py", "dash_member.py", "dash_dev.py", "database.py", "dev_chat.py"}
    for f in list(root.glob("*.py")) + list((root / "modules").glob("*.py")) + list((root / "discord_bot").rglob("*.py")):
        if f.name in allowed:
            continue
        src = f.read_text(errors="ignore")
        assert "ai_usage.consume" not in src and "ai_usage_consume" not in src, f


def test_route_is_rate_limited_per_minute(chat):
    codes = [send()[0] for _ in range(9)]
    assert codes[:8] == [200] * 8 and codes[8] == 429


def test_level_up_renderer_uses_the_custom_card_first_and_falls_back_safely():
    src = (Path(dash.__file__).resolve().parent.parent / "discord_bot" / "cogs" / "leveling.py").read_text()
    i = src.index("card_for_user")
    assert "except Exception" in src[i:i + 400] and "custom_card = None" in src[i:i + 500]      # lookup failure -> normal card
    assert src.index("if custom_card is not None:") < src.index("elif tier_image is not None:")  # paying member's card wins
