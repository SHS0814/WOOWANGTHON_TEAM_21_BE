"""하루·주간 요약 (api.md 3.25, backend.md 9장). 저장하지 않고 요청할 때 계산한다."""

from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DoseEvent, Family, Handoff, Reading
from app.services.dose import ALLOWED_VERDICTS
from app.services.expected import active_pillbox, latest_refill, mismatch_cells, refill_status
from app.timeutil import kst_date

PENDING = ("scheduled", "asked", "reading", "retake_requested", "reminded")


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def summarize(
    db: Session,
    settings: Settings,
    family: Family,
    period: str,
    start: date,
    now: datetime,
    refill: dict | None,
) -> dict:
    end = start if period == "day" else start + timedelta(days=6)
    events = db.scalars(
        select(DoseEvent).where(
            DoseEvent.family_id == family.id,
            DoseEvent.date >= start.isoformat(),
            DoseEvent.date <= end.isoformat(),
        )
    ).all()
    ids = [e.id for e in events]
    handoffs = (
        db.scalars(select(Handoff).where(Handoff.dose_event_id.in_(ids))).all() if ids else []
    )
    readings = (
        db.scalars(
            select(Reading).where(Reading.dose_event_id.in_(ids), Reading.kind == "cells")
        ).all()
        if ids
        else []
    )
    counts = {
        "scheduled": len(events),
        "taken_ai": sum(e.outcome == "taken" and e.outcome_source == "ai" for e in events),
        "taken_guardian": sum(
            e.outcome == "taken" and e.outcome_source == "guardian" for e in events
        ),
        "not_taken": sum(e.outcome == "not_taken" for e in events),
        "acknowledged": sum(e.outcome == "acknowledged" for e in events),
        "open_handoffs": sum(h.verdict is None for h in handoffs),
        "pending": sum(e.state in PENDING for e in events),
    }
    by_reason = Counter(h.reason for h in handoffs)
    return {
        "period": period,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "final": end < kst_date(now),
        "counts": counts,
        "handoffs_by_reason": {r: by_reason.get(r, 0) for r in ALLOWED_VERDICTS},
        "all_normal": bool(events)
        and counts["taken_ai"] == len(events)
        and counts["open_handoffs"] == 0,
        "refill": refill,
        "includes_mock": any(r.is_mock for r in readings),
        "includes_simulated": any(e.is_simulated for e in events),
        "pillbox_mismatch": any(mismatch_cells(r) for r in readings if r.cells),
    }


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
    items = []
    d = start if period == "day" else week_start(start)
    step = timedelta(days=1 if period == "day" else 7)
    while d <= end:
        items.append(summarize(db, settings, family, period, d, now, refill))
        d += step
    return items
