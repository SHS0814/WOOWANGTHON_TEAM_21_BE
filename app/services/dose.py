"""복약 건 생성 (backend.md 6장, 9장 create_dose_events)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DoseEvent
from app.services.expected import active_slots
from app.timeutil import at_kst, kst_date


def sync_events(db: Session, family_id: str, now: datetime) -> list[DoseEvent]:
    """오늘 일정 중 아직 복약 건이 없는 칸의 건을 만든다. 이미 있는 건은 그대로 둔다."""
    today = kst_date(now)
    slots = [s for s in active_slots(db, family_id) if s.weekday == today.weekday()]
    existing = set(
        db.scalars(
            select(DoseEvent.slot_id).where(
                DoseEvent.family_id == family_id, DoseEvent.date == today.isoformat()
            )
        )
    )
    created = []
    for slot in slots:
        if slot.id in existing:
            continue
        event = DoseEvent(
            family_id=family_id,
            slot_id=slot.id,
            date=today.isoformat(),
            scheduled_at=at_kst(today, slot.dose_time),
            state="scheduled",
        )
        db.add(event)
        created.append(event)
    return created
