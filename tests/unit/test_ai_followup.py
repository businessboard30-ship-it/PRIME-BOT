from modules import ai_followup as f

DRILL = 'check the server stats, lock #general, schedule a message in #announcements, then reopen #general'


def test_single_call_for_a_multi_part_request_gets_a_second_turn():
    assert f.needs_followup(DRILL, [{"name": "serveranalytics"}])


def test_no_second_turn_when_not_needed():
    assert not f.needs_followup(DRILL, [{"name": "a"}, {"name": "b"}])        # model already made several calls
    assert not f.needs_followup("lock #general", [{"name": "a"}])             # simple request
    assert not f.needs_followup(DRILL, [])


def test_second_turn_never_repeats_or_adds_junk():
    calls = [{"name": "serveranalytics"}, {"name": "skill__lockdown_channel", "arguments": {}}, {"x": 1}, "bad"]
    assert [c["name"] for c in f.remaining_calls(calls, ["serveranalytics"])] == ["skill__lockdown_channel"]
    assert f.remaining_calls(None, []) == []


def test_prompt_names_what_is_done():
    p = f.followup_prompt(DRILL, ["serveranalytics"])
    assert "serveranalytics" in p and "do NOT call these again" in p and DRILL in p


def test_wired_into_chat_turn_and_never_raises():
    src = open("discord_bot/cogs/ai_tools.py").read()
    assert "await self._second_turn(" in src and "except Exception:\n            logger.exception(\"[aichat] second turn" in src


def test_reply_and_mention_chat_also_get_the_second_turn():
    src = open("discord_bot/cogs/ai_tools.py").read()
    reply = src.split('kind="reply", tools=None if atts')[1].split("@commands.Cog.listener")[0]
    assert "await self._second_turn(" in reply and "real_interaction=False" in reply and '"kind": "reply"' in reply


def test_repeats_of_one_command_with_different_args_are_kept():
    first = [{"name": "role_create", "arguments": {"name": "A"}}]
    more = [{"name": "role_create", "arguments": {"name": "A"}}, {"name": "role_create", "arguments": {"name": "B"}},
            {"name": "role_create", "arguments": {"name": "C"}}]
    got = f.remaining_calls(more, f.done_names(first), first_calls=first)
    assert [c["arguments"]["name"] for c in got] == ["B", "C"]          # only the identical call is dropped
    assert f.remaining_calls(more, ["role_create"]) == []                # old name-only behaviour unchanged without first_calls


def test_a_twenty_part_request_with_one_call_still_gets_a_second_turn():
    twenty = "; ".join(f"create channel number {i}" for i in range(1, 21))
    assert f.needs_followup(twenty, [{"name": "serveranalytics"}])
    assert not f.needs_followup(twenty, [{"name": f"c{i}"} for i in range(20)])
