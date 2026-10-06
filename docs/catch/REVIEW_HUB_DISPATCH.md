# Review note: hub answered twice (plain screen could replace the drawn card)

## Bug
discord.py runs every matching DynamicItem AND the live view's own callback for the same click (`ViewStore.dispatch_view`
calls `dispatch_dynamic_items` first, then the view). The hub registers both: the restart-safe dynamic select and
buttons, and the live `CatchHubCardView`. So on a live hub every click was answered twice:
- the dynamic select edits the message to the plain hub embed with `attachments=[]`, which can win over the drawn card
  (this matches the plain "Catch hub" screenshots Maxwell sent after picking Info and Social);
- the dynamic Home button sent an extra plain hub message next to the card;
- actions (Encounter, Daily, ...) could run twice.
It was invisible before #89, because both answers drew the same plain screen.

## Fix
`live_view_owns(interaction)` in `catch.py` asks the library's view store whether a live view already holds this click
(same message id, component type and custom id). `CatchHubDynamicSelect.callback` and `CatchHubDynamicButton.callback`
return at once when it does. After a restart there is no live view, so the dynamic items answer exactly as before. If the
store layout differs in some discord.py version the guard returns False (old behaviour).

## Tests
`tests/unit/test_catch_hub_dispatch.py`: 11 tests, using a REAL `discord.ui.view.ViewStore` holding a real `CatchHubCardView`
(a library layout change fails there). No existing test was edited. 6 mutants checked, each trips a test.

## Not verified
Not run in live Discord. I could not drive a real click through discord.py's dispatch (the dynamic path needs a full
message with components), so the "both fire" claim comes from reading discord.py 2.7.1 source, and the screenshots fit it.
The requirement is `discord.py>=2.6.0`. After a restart the old plain hub still shows (that path is pinned by old tests).
