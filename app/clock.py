"""현재 시각. 테스트에서 시각을 고정할 수 있게 한 곳에서만 읽는다."""

from datetime import UTC, datetime


class Clock:
    def __init__(self) -> None:
        self._fixed: datetime | None = None

    def now(self) -> datetime:
        return self._fixed or datetime.now(UTC)

    def set(self, value: datetime | None) -> None:
        if value is not None and value.tzinfo is None:
            raise ValueError("clock needs an aware datetime")
        self._fixed = value.astimezone(UTC) if value else None


clock = Clock()
