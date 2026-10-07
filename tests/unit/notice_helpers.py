"""Helpers for tests that check refusal and error messages, which are now notice embeds.

Strict on purpose: both helpers need the embed, so a message that falls back to bare text fails.
"""


def body(call) -> str:
    """Notice text from one captured ``(args, kwargs)`` send."""
    return call[1]["embed"].description


def shown(args, kwargs) -> str:
    """Notice text from the ``(*args, **kwargs)`` a send mock received."""
    return kwargs["embed"].description
