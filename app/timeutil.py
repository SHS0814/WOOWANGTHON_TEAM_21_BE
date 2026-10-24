"""서울 시간 변환. DB에는 UTC, API에는 +09:00 오프셋으로 낸다 (api.md 1.3)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(KST).isoformat(timespec="seconds")


def kst_date(dt: datetime) -> date:
    return dt.astimezone(KST).date()


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def at_kst(d: date, hhmm: str) -> datetime:
    """서울 날짜 + HH:MM → UTC datetime."""
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=KST).astimezone(UTC)


def kst_day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), tzinfo=KST).astimezone(UTC)
    return start, start + timedelta(days=1)
