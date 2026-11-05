"""The AI must act on runnable requests instead of writing a plan and asking permission."""
from modules import command_reference as cr


def test_runnable_context_tells_the_model_to_act_not_ask():
    ctx = cr.build_context(runnable={"serveranalytics"})
    assert "ACT, don't ask" in ctx and "Confirm" in ctx and "call all the tools" in ctx


def test_describe_only_context_does_not_promise_actions():
    assert "ACT, don't ask" not in cr.build_context()


def test_action_words_attach_the_tools():
    for text in ("lock #general", "schedule a message", "announce this", "set up a raid drill", "unlock the channel"):
        assert cr.is_command_question(text), text
    assert not cr.is_command_question("hey what's up")
