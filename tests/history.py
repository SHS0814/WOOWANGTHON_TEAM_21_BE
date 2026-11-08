"""기록 검색·요약 테스트와 응답 시간 측정에 쓰는 과거 복약 기록 (DB 직접 입력).

날짜마다 그 요일의 일정 칸으로 복약 건을 만든다. 건 번호 i 기준 규칙:
- i % 10 == 3: 무응답 인계 → 보호자 판정 not_taken
- i % 10 == 5: 판독 애매 인계 → 보호자 판정 taken (판독 1개, 약통 불일치 칸 포함)
- i % 10 == 7: 인계 없이 not_taken
- i % 17 == 0: 리필 확인 요청(binds_event=False) 인계 추가, 판정 refilled (결과는 위 규칙 그대로)
- i % 50 == 9: 무응답 인계가 아직 열려 있음 (outcome 없음)
- 그 밖: AI 정상 판독 taken (모의 판독, 사진 1장)
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import Device, DoseEvent, Handoff, Photo, Reading, ScheduleSlot
from app.timeutil import KST


def _cells(target: str, mismatch: bool) -> list[dict]:
    cells = [{"cell_id": target, "ai_state": "empty", "expected": "empty", "attention": None}]
    if mismatch:
        cells.append(
            {
                "cell_id": "r0c0",
                "ai_state": "empty",
                "expected": "full",
                "attention": "unexpected_empty",
            }
        )
    return cells


def seed_history(db, family_id: str, start: date, end: date) -> list[DoseEvent]:
    slots = list(
        db.scalars(
            select(ScheduleSlot).where(ScheduleSlot.family_id == family_id, ScheduleSlot.active)
        )
    )
    elder = db.scalar(select(Device).where(Device.family_id == family_id, Device.role == "elder"))
    events: list[DoseEvent] = []
    i = 0
    d = start
    while d <= end:
        for slot in sorted(
            (s for s in slots if s.weekday == d.weekday()), key=lambda s: s.dose_time
        ):
            hh, mm = map(int, slot.dose_time.split(":"))
            at = datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)
            e = DoseEvent(
                family_id=family_id,
                slot_id=slot.id,
                date=d.isoformat(),
                scheduled_at=at,
                state="resolved",
                resolved_at=at + timedelta(minutes=40),
            )
            db.add(e)
            db.flush()
            kind = i % 10
            if i % 50 == 9:
                e.state = "handed_off"
                db.add(
                    Handoff(
                        family_id=family_id,
                        dose_event_id=e.id,
                        reason="no_response",
                        notified_at=at + timedelta(minutes=30),
                    )
                )
            elif kind == 3:
                e.outcome, e.outcome_source = "not_taken", "guardian"
                db.add(
                    Handoff(
                        family_id=family_id,
                        dose_event_id=e.id,
                        reason="no_response",
                        notified_at=at + timedelta(minutes=30),
                        verdict="not_taken",
                        verdict_at=at + timedelta(minutes=50),
                    )
                )
            elif kind == 7:
                e.outcome, e.outcome_source = "not_taken", "guardian"
            else:
                p = Photo(
                    family_id=family_id,
                    dose_event_id=e.id,
                    path="x",
                    media_type="image/png",
                    taken_at=at + timedelta(minutes=3),
                    uploaded_at=at + timedelta(minutes=3),
                    uploaded_by_device_id=elder.id,
                )
                db.add(p)
                db.flush()
                r = Reading(
                    family_id=family_id,
                    photo_id=p.id,
                    dose_event_id=e.id,
                    kind="cells",
                    provider="mock",
                    model="mock-v1",
                    is_mock=True,
                    output={},
                    cells=_cells(slot.cell_id, kind == 5),
                    target_cell_id=slot.cell_id,
                    classification="unclear" if kind == 5 else "normal",
                    created_at=at + timedelta(minutes=4),
                )
                db.add(r)
                db.flush()
                e.taken_at = at + timedelta(minutes=3)
                if kind == 5:
                    e.outcome, e.outcome_source = "taken", "guardian"
                    db.add(
                        Handoff(
                            family_id=family_id,
                            dose_event_id=e.id,
                            reason="unclear",
                            reading_id=r.id,
                            notified_at=at + timedelta(minutes=5),
                            verdict="taken",
                            verdict_at=at + timedelta(minutes=20),
                        )
                    )
                else:
                    e.outcome, e.outcome_source = "taken", "ai"
            if i % 17 == 0:
                db.add(
                    Handoff(
                        family_id=family_id,
                        dose_event_id=e.id,
                        reason="refill_unconfirmed",
                        notified_at=at + timedelta(minutes=1),
                        binds_event=False,
                        verdict="refilled",
                        verdict_at=at + timedelta(minutes=2),
                    )
                )
            events.append(e)
            i += 1
        d += timedelta(days=1)
    db.commit()
    return events
