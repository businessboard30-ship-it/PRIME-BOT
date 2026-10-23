# path: modules/log_throttle.py

"""
Console log throttle: the same WARNING/ERROR line (same logger, level, message and exception type) is printed once
per window; repeats inside the window are counted and summed up on the next print as "(+N similar suppressed)".

Why: a poller that fails every few seconds (database down, missing permission) used to print a full traceback on
every tick, on every clone, which floods Railway's log storage and costs CPU/IO for no extra information.

Only the console handler is filtered. The owner panel's log buffer (modules/admin_ops.install_log_buffer) still sees
every record. INFO and DEBUG are never touched. Never raises: any problem lets the record through.
Switch off with LOG_THROTTLE=0; window with LOG_THROTTLE_SECONDS (default 60, clamped 5-3600).
"""

import logging
import os
import threading
import time

MAX_KEYS = 500


def _window() -> float:
    try:
        return min(3600.0, max(5.0, float(os.getenv("LOG_THROTTLE_SECONDS", "60"))))
    except ValueError:
        return 60.0


class ThrottleFilter(logging.Filter):
    def __init__(self, window: float | None = None, clock=time.monotonic):
        super().__init__()
        self.window = window if window is not None else _window()
        self._clock = clock
        self._seen: dict[tuple, list] = {}      # key -> [first_seen, suppressed_count]
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.levelno < logging.WARNING:
                return True
            exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
            key = (record.name, record.levelno, str(record.msg)[:200], exc)
            now = self._clock()
            with self._lock:
                entry = self._seen.get(key)
                if entry and now - entry[0] < self.window:
                    entry[1] += 1
                    return False
                suppressed = entry[1] if entry else 0
                self._seen[key] = [now, 0]
                if len(self._seen) > MAX_KEYS:
                    cutoff = now - self.window
                    self._seen = {k: v for k, v in self._seen.items() if v[0] >= cutoff}
            if suppressed:
                record.msg = f"{record.msg} (+{suppressed} similar suppressed)"
            return True
        except Exception:
            return True  # logging filter: never log from here, it would re-enter this filter


def install() -> None:
    """Adds the filter to the root logger's console handlers. Safe to call twice."""
    if os.getenv("LOG_THROTTLE", "1") == "0":
        return
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            continue
        if not any(isinstance(f, ThrottleFilter) for f in handler.filters):
            handler.addFilter(ThrottleFilter())
