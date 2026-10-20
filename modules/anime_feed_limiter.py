# path: modules/anime_feed_limiter.py

"""
One shared AniList request limiter for the anime feed (AniList allows about 90 requests a minute).

The posting worker has priority: it may use the whole budget, while user actions (Search, Categories, Read more) may only use
the part above a reserve, so a burst of searching can never starve the schedule fetch. A denied request is not queued or
retried: the caller says "busy" and the person tries again in a moment.
"""

from __future__ import annotations

import time
from collections import deque

PER_MINUTE = 90
WORKER_RESERVE = 30                 # requests per minute that only the worker may use


class Limiter:
    def __init__(self, per_minute: int = PER_MINUTE, reserve: int = WORKER_RESERVE):
        self.per_minute, self.reserve = per_minute, reserve
        self._hits: "deque[float]" = deque()

    def try_acquire(self, priority: bool = False) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] >= 60.0:
            self._hits.popleft()
        cap = self.per_minute if priority else max(0, self.per_minute - self.reserve)
        if len(self._hits) >= cap:
            return False
        self._hits.append(now)
        return True


anilist = Limiter()
