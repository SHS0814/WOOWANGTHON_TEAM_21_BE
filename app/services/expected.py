"""기대 상태와 리필 현황 (backend.md 5장, R-010 2·3·5번).

기대 상태: 마지막 리필 기준선에서 시작해, 그 뒤 복약 건의 결과를 칸에 반영한다.
- taken → empty
- 인계됐거나 not_taken / acknowledged → ignored (비교 제외, R-007 C2)
- 리필 확인 "아니요"로 뺀 칸 → ignored (R-010 3번)
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DoseEvent, Pillbox, Reading, Refill, ScheduleSlot
from app.timeutil import at_kst, iso, kst_date

RESOLVED_STATES = ("done", "handed_off")


def active_pillbox(db: Session, family_id: str) -> Pillbox | None:
    return db.scalar(select(Pillbox).where(Pillbox.family_id == family_id, Pillbox.active))


def active_slots(db: Session, family_id: str) -> list[ScheduleSlot]:
    return list(
        db.scalars(
            select(ScheduleSlot)
            .where(ScheduleSlot.family_id == family_id, ScheduleSlot.active)
            .order_by(ScheduleSlot.weekday, ScheduleSlot.dose_time)
        )
    )


def latest_refill(db: Session, pillbox_id: str) -> Refill | None:
    return db.scalar(
        select(Refill)
        .where(Refill.pillbox_id == pillbox_id)
        .order_by(Refill.filled_at.desc(), Refill.created_at.desc())
        .limit(1)
    )


def expected_states(
    db: Session,
    refill: Refill,
    *,
    before: datetime | None = None,
    exclude_event_id: str | None = None,
) -> dict[str, str]:
    """칸별 기대 상태 (full | empty | ignored). before 이전 복약 건만 반영한다."""
    states = {c["cell_id"]: c["state"] for c in refill.baseline_cells}
    q = (
        select(DoseEvent, ScheduleSlot.cell_id)
        .join(ScheduleSlot, ScheduleSlot.id == DoseEvent.slot_id)
        .where(
            DoseEvent.family_id == refill.family_id,
            ScheduleSlot.pillbox_id == refill.pillbox_id,
            DoseEvent.scheduled_at > refill.filled_at,
        )
        .order_by(DoseEvent.scheduled_at)
    )
    if before is not None:
        q = q.where(DoseEvent.scheduled_at < before)
    for event, cell_id in db.execute(q):
        if event.id == exclude_event_id or cell_id not in states:
            continue
        if event.outcome == "taken":
            states[cell_id] = "empty"
        elif event.state == "handed_off" or event.outcome in ("not_taken", "acknowledged"):
            states[cell_id] = "ignored"
    # 리필 확인 "아니요"로 뺀 칸은 복약 기록보다 우선한다 (R-010 3번)
    for cid in refill.ignored_cells or []:
        if cid in states:
            states[cid] = "ignored"
    return states


def doses_per_day(slots: list[ScheduleSlot]) -> int:
    per_day = Counter(s.weekday for s in slots)
    return max(per_day.values(), default=0)


@dataclass
class RefillStatus:
    remaining_cells: int
    covered_until: dict | None
    needs_refill: bool
    threshold_cells: int

    def out(self) -> dict:
        return {
            "remaining_cells": self.remaining_cells,
            "covered_until": self.covered_until,
            "needs_refill": self.needs_refill,
            "threshold_cells": self.threshold_cells,
        }


def refill_status(
    db: Session, settings: Settings, family_id: str, refill: Refill | None, now: datetime
) -> RefillStatus:
    slots = active_slots(db, family_id)
    threshold = doses_per_day(slots) * settings.refill_threshold_days
    if refill is None or not slots:
        return RefillStatus(0, None, threshold > 0, threshold)
    states = expected_states(db, refill)
    slot_cells = {s.cell_id for s in slots}
    remaining = sum(1 for cid, st in states.items() if st == "full" and cid in slot_cells)

    resolved = {
        (e.slot_id, e.date)
        for e in db.scalars(
            select(DoseEvent).where(
                DoseEvent.family_id == family_id,
                DoseEvent.state.in_(RESOLVED_STATES),
                DoseEvent.date >= kst_date(now).isoformat(),
            )
        )
    }
    grace = timedelta(minutes=settings.no_response_handoff_min)
    covered = None
    used: set[str] = set()
    today = kst_date(now)
    for offset in range(8):
        d = today + timedelta(days=offset)
        for slot in sorted(
            (s for s in slots if s.weekday == d.weekday()), key=lambda s: s.dose_time
        ):
            occ = at_kst(d, slot.dose_time)
            if occ < now - grace or (slot.id, d.isoformat()) in resolved:
                continue
            if slot.cell_id in used or states.get(slot.cell_id) != "full":
                return RefillStatus(remaining, covered, remaining <= threshold, threshold)
            used.add(slot.cell_id)
            covered = {
                "date": d.isoformat(),
                "weekday": slot.weekday,
                "time_of_day": slot.time_of_day,
                "dose_time": slot.dose_time,
            }
    return RefillStatus(remaining, covered, remaining <= threshold, threshold)


def pillbox_mismatch(db: Session, refill: Refill | None) -> dict:
    """마지막 리필 이후 가장 최근 복약 판독에서 대상 칸 외 불일치 (R-010 2번)."""
    empty = {"detected": False, "cell_ids": [], "reading_id": None, "detected_at": None}
    if refill is None:
        return empty
    reading = db.scalar(
        select(Reading)
        .where(
            Reading.family_id == refill.family_id,
            Reading.kind == "cells",
            Reading.dose_event_id.is_not(None),
            Reading.error.is_(None),
            Reading.created_at > refill.created_at,
        )
        .order_by(Reading.created_at.desc())
        .limit(1)
    )
    if reading is None:
        return empty
    cells = mismatch_cells(reading)
    if not cells:
        return {**empty, "reading_id": reading.id}
    return {
        "detected": True,
        "cell_ids": cells,
        "reading_id": reading.id,
        "detected_at": iso(reading.created_at),
    }


def mismatch_cells(reading: Reading) -> list[str]:
    return [
        c["cell_id"]
        for c in reading.cells
        if c["cell_id"] != reading.target_cell_id
        and c.get("attention") in ("unexpected_empty", "unexpected_full")
    ]
