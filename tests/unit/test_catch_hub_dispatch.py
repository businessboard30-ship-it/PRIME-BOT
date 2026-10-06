"""discord.py runs the hub's DynamicItems AND the live view for the same click. The dynamic items must
stand down when a live view has the click, or the plain screen can replace the drawn hub card."""
import asyncio
from types import SimpleNamespace

import discord
from discord.ui.view import ViewStore

from discord_bot.cogs import catch as cog

MESSAGE_ID = 555
SELECT_ID = "catch:hub:category"
SELECT = 3   # discord.ComponentType.select
BUTTON = 2   # discord.ComponentType.button


def run(coro):
    return asyncio.run(coro)


def live_store(message_id=MESSAGE_ID):
    """A REAL discord.py ViewStore holding a REAL hub card view (so a library layout change fails here)."""
    async def build():
        store = ViewStore(SimpleNamespace())
        view = cog.CatchHubCardView()
        store.add_view(view, message_id)
        return store, view

    return run(build())


def click(store, custom_id=SELECT_ID, component_type=SELECT, message_id=MESSAGE_ID, **extra):
    calls = []

    async def record(name, *a, **k):
        calls.append((name, a, k))

    async def edit_message(*a, **k):
        await record("edit_message", *a, **k)

    async def send_message(*a, **k):
        await record("send_message", *a, **k)

    async def defer(*a, **k):
        await record("defer", *a, **k)

    inter = SimpleNamespace(
        client=SimpleNamespace(_connection=SimpleNamespace(_view_store=store), clone_id=None),
        data={"component_type": component_type, "custom_id": custom_id, "values": ["social"], **extra},
        message=None if message_id is None else SimpleNamespace(id=message_id),
        response=SimpleNamespace(edit_message=edit_message, send_message=send_message, defer=defer),
        user=SimpleNamespace(id=7), guild_id=1,
    )
    return inter, calls


# ---------------------------------------------------------------- the guard

def test_the_guard_sees_the_real_view_store_layout():
    store, view = live_store()
    assert (SELECT, SELECT_ID) in store._views[MESSAGE_ID]      # the layout the guard reads
    inter, _ = click(store)
    assert cog.live_view_owns(inter) is True


def test_every_live_hub_button_is_owned_by_the_live_view():
    store, view = live_store()
    ids = [c.custom_id for c in view.children if isinstance(c, discord.ui.Button)]
    assert ids
    for custom_id in ids:
        inter, _ = click(store, custom_id, BUTTON)
        assert cog.live_view_owns(inter) is True, custom_id


def test_the_guard_says_no_for_a_message_without_a_live_view():
    store, _ = live_store()
    inter, _ = click(store, message_id=999)           # e.g. a hub message from before a restart
    assert cog.live_view_owns(inter) is False


def test_the_guard_says_no_for_an_unknown_component_or_type_or_missing_message():
    store, _ = live_store()
    assert cog.live_view_owns(click(store, "catch:hub:nope")[0]) is False
    assert cog.live_view_owns(click(store, SELECT_ID, BUTTON)[0]) is False
    assert cog.live_view_owns(click(store, message_id=None)[0]) is False


def test_the_guard_says_no_once_the_view_is_gone():
    store, view = live_store()
    store.remove_view(view)
    assert cog.live_view_owns(click(store)[0]) is False


def test_the_guard_fails_open_when_the_library_internals_are_missing():
    inter, _ = click(live_store()[0])
    inter.client = SimpleNamespace(clone_id=None)      # no _connection at all
    assert cog.live_view_owns(inter) is False
    assert cog.live_view_owns(SimpleNamespace()) is False


# ---------------------------------------------------------------- the dynamic items

def test_the_dynamic_select_stands_down_for_a_live_hub():
    inter, calls = click(live_store()[0])
    run(cog.CatchHubDynamicSelect().callback(inter))
    assert calls == []


def test_the_dynamic_select_still_answers_after_a_restart_and_clears_the_card():
    inter, calls = click(live_store()[0], message_id=999)
    run(cog.CatchHubDynamicSelect().callback(inter))
    assert [c[0] for c in calls] == ["edit_message"]
    assert calls[0][2]["attachments"] == [] and isinstance(calls[0][2]["view"], cog.CatchHubView)


def _button(action, custom_id):
    return cog.CatchHubDynamicButton(discord.ui.Button(custom_id=custom_id), action=action)


def test_the_dynamic_home_button_does_not_add_a_plain_hub_next_to_the_live_one():
    inter, calls = click(live_store()[0], "catch:hub:home", BUTTON)
    run(_button("home", "catch:hub:home").callback(inter))
    assert calls == []


def test_the_dynamic_home_button_still_sends_a_hub_after_a_restart():
    inter, calls = click(live_store()[0], "catch:hub:home", BUTTON, message_id=999)
    run(_button("home", "catch:hub:home").callback(inter))
    assert [c[0] for c in calls] == ["send_message"]


def test_dynamic_actions_do_not_run_a_second_time_beside_the_live_view(monkeypatch):
    ran = []

    async def fake_action(interaction):
        ran.append("action")

    monkeypatch.setitem(cog.REAL_ACTIONS, "daily", fake_action)
    store, _ = live_store()
    inter, _ = click(store, "catch:hub:daily", BUTTON)
    run(_button("daily", "catch:hub:daily").callback(inter))
    assert ran == []
    inter, _ = click(store, "catch:hub:daily", BUTTON, message_id=999)
    run(_button("daily", "catch:hub:daily").callback(inter))
    assert ran == ["action"]
