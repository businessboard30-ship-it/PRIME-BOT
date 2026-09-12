# path: discord_bot/cogs/_adaptive_skip.py

"""
Every clone process runs its own copy of every background tasks.loop
poller (bump, schedule, server listings, report notifications, etc.),
each firing on a fixed timer FOREVER, whether or not any server on that
clone actually uses the feature. A clone with zero bump listings still
asks the database "any bump reminders due?" every few seconds, all day,
every day — pure wasted database egress that scales with the number of
clones, not with actual usage.

AdaptiveSkip fixes this without needing any new "is this feature used"
lookup, new database method, or schema change — it just watches the
SAME due/queue result the loop was already fetching:

  - If a fetch keeps coming back empty for `idle_threshold` ticks in a
    row, the feature is treated as idle for this clone right now, and
    the loop skips its next `idle_skip` ticks without hitting the
    database at all.
  - The instant something real does need to run (someone schedules a
    message, a bump becomes due, etc.), it writes straight to the
    database as normal — nothing about this changes; only the empty
    "check if there's anything to do" polling backs off.
  - Every `idle_skip` ticks it checks again on its own, so nothing can
    get permanently stuck — worst case something due fires up to
    `idle_skip` ticks later than it would have before, which for
    background jobs on this timescale (seconds/minutes) is unnoticeable.

Usage inside a @tasks.loop function:

    self._gate = AdaptiveSkip(idle_threshold=5, idle_skip=5)
    ...
    @tasks.loop(seconds=45)
    async def _poller(self):
        if not self._gate.should_run():
            return
        due = await db.get_due_whatever(...)
        self._gate.record(found_something=bool(due))
        ... existing loop body using `due` ...
"""


class AdaptiveSkip:
    def __init__(self, idle_threshold: int = 5, idle_skip: int = 5):
        """idle_threshold: how many consecutive empty ticks before we
        start skipping. idle_skip: how many ticks to skip once idle,
        before checking again."""
        self._idle_threshold = idle_threshold
        self._idle_skip = idle_skip
        self._empty_streak = 0
        self._skip_remaining = 0

    def should_run(self) -> bool:
        if self._skip_remaining > 0:
            self._skip_remaining -= 1
            return False
        return True

    def record(self, found_something: bool) -> None:
        if found_something:
            self._empty_streak = 0
            self._skip_remaining = 0
            return
        self._empty_streak += 1
        if self._empty_streak >= self._idle_threshold:
            self._skip_remaining = self._idle_skip
