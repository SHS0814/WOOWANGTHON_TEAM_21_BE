"""하루·주간·전체 요약 (api.md 3.25, backend.md 9장). 저장하지 않고 요청할 때 계산한다.

기간 전체의 복약 건·인계·판독을 쿼리 세 번으로 한꺼번에 읽은 뒤 날짜별로 나눠 계산한다
(R-051: 하루 요약 최대 366일).
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.config import Settings
from app.models import DoseEvent, Family, Handoff, Reading
from app.services.dose import ALLOWED_VERDICTS
from app.services.expected import active_pillbox, latest_refill, mismatch_cells, refill_status
from app.timeutil import kst_date

PENDING = ("scheduled", "asked", "reading", "retake_requested", "reminded")


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


class _Day:
    """하루치 원자료에서 요약에 필요한 값만 모은 것. 여러 날을 더해 주간·전체를 만든다."""

    __slots__ = ("by_reason", "counts", "mismatch", "mock", "n", "simulated")

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.by_reason: Counter = Counter()
        self.n = 0
        self.mock = False
        self.simulated = False
        self.mismatch = False

    def add(self, other: "_Day") -> None:
        self.counts.update(other.counts)
        self.by_reason.update(other.by_reason)
        self.n += other.n
        self.mock |= other.mock
        self.simulated |= other.simulated
        self.mismatch |= other.mismatch


def _load_days(db: Session, family_id: str, start: date, end: date) -> dict[str, _Day]:
    """[start, end] 의 복약 건을 날짜(YYYY-MM-DD)별로 모은다. 쿼리 세 번."""
    in_range = (
        DoseEvent.family_id == family_id,
        DoseEvent.date >= start.isoformat(),
        DoseEvent.date <= end.isoformat(),
    )
    days: dict[str, _Day] = defaultdict(_Day)
    date_of: dict[str, str] = {}
    for e in db.execute(
        select(
            DoseEvent.id,
            DoseEvent.date,
            DoseEvent.state,
            DoseEvent.outcome,
            DoseEvent.outcome_source,
            DoseEvent.is_simulated,
        ).where(*in_range)
    ):
        date_of[e.id] = e.date
        d = days[e.date]
        d.n += 1
        c = d.counts
        c["taken_ai"] += e.outcome == "taken" and e.outcome_source == "ai"
        c["taken_guardian"] += e.outcome == "taken" and e.outcome_source == "guardian"
        c["not_taken"] += e.outcome == "not_taken"
        c["acknowledged"] += e.outcome == "acknowledged"
        c["pending"] += e.state in PENDING
        d.simulated |= bool(e.is_simulated)
    if not date_of:
        return days
    for h in db.execute(
        select(Handoff.dose_event_id, Handoff.reason, Handoff.verdict)
        .join(DoseEvent, DoseEvent.id == Handoff.dose_event_id)
        .where(*in_range)
    ):
        d = days[date_of[h.dose_event_id]]
        d.counts["open_handoffs"] += h.verdict is None
        d.by_reason[h.reason] += 1
    for r in db.scalars(
        select(Reading)
        .options(
            load_only(Reading.dose_event_id, Reading.is_mock, Reading.cells, Reading.target_cell_id)
        )
        .join(DoseEvent, DoseEvent.id == Reading.dose_event_id)
        .where(*in_range, Reading.kind == "cells")
    ):
        d = days[date_of[r.dose_event_id]]
        d.mock |= bool(r.is_mock)
        if not d.mismatch and r.cells and mismatch_cells(r):
            d.mismatch = True
    return days


def _item(period: str, start: date, end: date, agg: _Day, refill, final: bool) -> dict:
    c = agg.counts
    counts = {
        "scheduled": agg.n,
        "taken_ai": c["taken_ai"],
        "taken_guardian": c["taken_guardian"],
        "not_taken": c["not_taken"],
        "acknowledged": c["acknowledged"],
        "open_handoffs": c["open_handoffs"],
        "pending": c["pending"],
    }
    return {
        "period": period,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "final": final,
        "counts": counts,
        "handoffs_by_reason": {r: agg.by_reason.get(r, 0) for r in ALLOWED_VERDICTS},
        "all_normal": agg.n > 0 and counts["taken_ai"] == agg.n and counts["open_handoffs"] == 0,
        "refill": refill,
        "includes_mock": agg.mock,
        "includes_simulated": agg.simulated,
        "pillbox_mismatch": agg.mismatch,
    }


def _span(days: dict[str, _Day], start: date, end: date) -> _Day:
    agg = _Day()
    d = start
    while d <= end:
        day = days.get(d.isoformat())
        if day is not None:
            agg.add(day)
        d += timedelta(days=1)
    return agg


def summarize(
    db: Session,
    settings: Settings,
    family: Family,
    period: str,
    start: date,
    now: datetime,
    refill: dict | None,
) -> dict:
    """하루(day) 또는 주간(week, start=월요일) 요약 한 항목."""
    end = start if period == "day" else start + timedelta(days=6)
    days = _load_days(db, family.id, start, end)
    return _item(period, start, end, _span(days, start, end), refill, end < kst_date(now))


def current_refill(db: Session, settings: Settings, family: Family, now: datetime) -> dict | None:
    pb = active_pillbox(db, family.id)
    if pb is None:
        return None
    st = refill_status(db, settings, family.id, latest_refill(db, pb.id), now)
    return {
        "needs_refill": st.needs_refill,
        "remaining_cells": st.remaining_cells,
        "covered_until": st.covered_until,
    }


def summaries(
    db: Session,
    settings: Settings,
    family: Family,
    period: str,
    start: date,
    end: date,
    now: datetime,
) -> list[dict]:
    refill = current_refill(db, settings, family, now)
    today = kst_date(now)
    first = start if period == "day" else week_start(start)
    step = timedelta(days=1 if period == "day" else 7)
    span = timedelta(days=0 if period == "day" else 6)
    # 마지막 주는 end 뒤 일요일까지 걸친다
    last_end = (end if period == "day" else week_start(end)) + span
    days = _load_days(db, family.id, first, last_end)
    items = []
    d = first
    while d <= end:
        e = d + span
        items.append(_item(period, d, e, _span(days, d, e), refill, e < today))
        d += step
    return items


def total_summary(db: Session, settings: Settings, family: Family, now: datetime) -> dict:
    """전체 기간 합계 (R-051). 첫 복약 건 날짜(없으면 오늘) ~ 오늘."""
    today = kst_date(now)
    first = db.scalar(
        select(func.min(DoseEvent.date)).where(
            DoseEvent.family_id == family.id, DoseEvent.date <= today.isoformat()
        )
    )
    start = date.fromisoformat(first) if first else today
    days = _load_days(db, family.id, start, today)
    agg = _Day()
    for day in days.values():
        agg.add(day)
    refill = current_refill(db, settings, family, now)
    return _item("total", start, today, agg, refill, False)
