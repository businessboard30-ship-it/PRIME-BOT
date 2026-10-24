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
import logging

logger = logging.getLogger(__name__)


class AdaptiveSkip:
    def __init__(self, idle_threshold: int = 5, idle_skip: int = 5, max_skip: int | None = None,
                 name: str | None = None):
        """idle_threshold: how many consecutive empty ticks before we
        start skipping. idle_skip: how many ticks to skip once idle,
        before checking again. max_skip (optional): when set, every
        further empty check doubles the skip (idle_skip, 2x, 4x ...) up
        to this many ticks, so a feature nobody uses keeps sleeping
        deeper; any real work resets it to full speed. name (optional):
        key from modules.sleep_events.LABELS; when given, falling asleep
        and waking up are reported to the support-server sleep log."""
        self._name = name
        self._announced = False
        self._idle_threshold = idle_threshold
        self._idle_skip = idle_skip
        self._max_skip = max_skip if max_skip and max_skip > idle_skip else None
        self._empty_streak = 0
        self._skip_remaining = 0
        self._next_skip = idle_skip

    @property
    def sleeping(self) -> bool:
        return self._skip_remaining > 0

    def should_run(self) -> bool:
        if self._skip_remaining > 0:
            self._skip_remaining -= 1
            return False
        return True

    def wake(self) -> None:
        """Back to full speed right away (e.g. a command just created work)."""
        self._empty_streak = 0
        self._skip_remaining = 0
        self._next_skip = self._idle_skip
        self._report_wake()

    def _report(self, kind: str, text: str) -> None:
        if not self._name:
            return
        try:
            from modules import sleep_events
            sleep_events.emit(kind, self._name, text.format(label=sleep_events.label(self._name)))
        except Exception:
            logger.debug("swallowed exception in AdaptiveSkip._report", exc_info=True)
            pass

    def _report_wake(self) -> None:
        if self._announced:
            self._announced = False
            self._report("wake", "☀️ **{label}** woke up: there is work to do again, back to normal speed.")

    def record(self, found_something: bool) -> None:
        if found_something:
            self.wake()
            return
        self._empty_streak += 1
        if self._empty_streak >= self._idle_threshold:
            if not self._announced:
                self._announced = True
                self._report("sleep", "😴 **{label}** went to sleep: nothing to do for a while, so it now checks less often.")
            self._skip_remaining = self._next_skip
            if self._max_skip:
                self._next_skip = min(self._next_skip * 2, self._max_skip)
