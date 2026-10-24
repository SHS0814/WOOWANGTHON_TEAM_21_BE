"""IP별 간단한 속도 제한 (프로세스 메모리). 서버가 한 대라 이걸로 충분하다."""

from collections import defaultdict, deque
from datetime import datetime, timedelta

from app.errors import ApiError


class SlidingWindow:
    def __init__(self, limit: int, window: timedelta) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        q = self._hits[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def check(self, key: str, now: datetime) -> None:
        q = self._prune(key, now)
        if len(q) >= self.limit:
            retry = int((q[0] + self.window - now).total_seconds()) + 1
            raise ApiError(
                429, "rate_limited", "요청이 너무 많음", headers={"Retry-After": str(retry)}
            )

    def hit(self, key: str, now: datetime) -> None:
        self._prune(key, now).append(now)

    def reset(self) -> None:
        self._hits.clear()
