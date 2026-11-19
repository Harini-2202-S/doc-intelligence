"""Sliding-window rate limiter.

State is held in memory, so limits apply per process. A multi-process deployment needs a
shared backend such as Redis behind the same interface.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def allow_global(self, name: str, limit: int, window_seconds: int) -> bool:
        """Service-wide limit; a limit of 0 or less means unlimited."""
        return limit <= 0 or self.allow(f"global:{name}", limit, window_seconds)
