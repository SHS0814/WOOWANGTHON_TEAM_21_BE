"""복약 건 상태 흐름 (backend.md 6·7장, R-010, R-014).

모든 상태 전이는 여기서만 한다. 예약 작업(app/jobs.py)과 API가 같은 함수를 쓴다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DoseEvent, Family, Handoff, Photo, Reading, Refill, ScheduleSlot
from app.push import PushSender, notify_guardians
from app.reader import Layout, Reader
from app.services import readings as rs
from app.services.classify import classify
from app.services.expected import active_pillbox, active_slots, expected_states, latest_refill
from app.timeutil import at_kst, kst_date

OPEN_STATES = ("asked", "retake_requested", "reminded")
TIME_OF_DAY_KO = {"morning": "아침", "noon": "점심", "evening": "저녁", "bedtime": "취침"}

ALLOWED_VERDICTS = {
    "no_response": ["taken", "not_taken", "acknowledged"],
    "unclear": ["taken", "not_taken"],
    "unchanged": ["taken", "not_taken", "acknowledged"],
    "double_suspected": ["taken", "not_taken", "acknowledged"],
    "refill_unconfirmed": ["refilled", "not_refilled"],
    "refill_overdue": ["taken", "not_taken", "acknowledged"],
}

# 보호자 푸시 문구 (보호자용이라 서버 템플릿, api.md 4.3)
PUSH_BODY = {
    "no_response": "{elder} {tod} 약 시간에 응답이 없어요",
    "unclear": "{elder} {tod} 약 사진을 확인해 주세요",
    "unchanged": "{elder} {tod} 약 칸이 그대로예요",
    "double_suspected": "{elder} 약통에서 다른 칸이 비어 있어요",
    "refill_unconfirmed": "약통을 채우셨나요? 확인해 주세요",
    "refill_overdue": "약통이 비어 있어 {elder} {tod} 약은 확인할 수 없어요. 약통을 채워 주세요",
}


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
        try:
            with db.begin_nested():  # 다른 요청이 먼저 만들었으면 유니크 인덱스에 걸린다
                db.add(event)
        except IntegrityError:
            continue
        created.append(event)
    return created


def handoff_deadline(settings: Settings, event: DoseEvent) -> datetime:
    return event.scheduled_at + timedelta(minutes=settings.no_response_handoff_min)


def create_handoff(
    db: Session,
    push: PushSender,
    family: Family,
    event: DoseEvent | None,
    reason: str,
    now: datetime,
    *,
    reading: Reading | None = None,
    binds_event: bool = True,
    send_push: bool = True,
    dose_event_id: str | None = None,
) -> Handoff | None:
    """인계를 만든다. 같은 원인의 열린 인계가 이미 있으면(동시 요청) 만들지 않고 None.

    유니크 인덱스(models.py)가 중복을 막고, 푸시는 커밋 뒤에 보낸다 (R-007 C2, R-018).
    """
    h = Handoff(
        family_id=family.id,
        dose_event_id=event.id if event else dose_event_id,
        reason=reason,
        reading_id=reading.id if reading else None,
        notified_at=now,
        binds_event=binds_event,
        pushed=send_push,
    )
    try:
        with db.begin_nested():
            db.add(h)
    except IntegrityError:
        return None
    if event is not None and binds_event:
        event.state = "handed_off"
        event.retake_deadline_at = None
    db.flush()
    if send_push:
        slot = db.get(ScheduleSlot, h_event_slot(db, h))
        tod = TIME_OF_DAY_KO.get(slot.time_of_day, "") if slot else ""
        notify_guardians(
            db,
            push,
            family.id,
            title="복약 확인",
            body=PUSH_BODY[reason].format(elder=family.elder_display_name, tod=tod),
            data={"type": "handoff", "handoff_id": h.id, "reason": reason},
        )
    return h


def h_event_slot(db: Session, h: Handoff) -> str | None:
    event = db.get(DoseEvent, h.dose_event_id)
    return event.slot_id if event else None


def open_bound_handoff(db: Session, event_id: str) -> Handoff | None:
    return db.scalar(
        select(Handoff)
        .where(
            Handoff.dose_event_id == event_id,
            Handoff.binds_event,
            Handoff.verdict.is_(None),
        )
        .order_by(Handoff.notified_at.desc())
        .limit(1)
    )


def lock_event(db: Session, event_id: str) -> DoseEvent | None:
    """복약 건 행을 잠그고 다시 읽는다 (PostgreSQL FOR UPDATE, SQLite 는 쓰기 잠금으로 대신)."""
    return db.scalar(
        select(DoseEvent)
        .where(DoseEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def tick(db: Session, settings: Settings, push: PushSender, family: Family, now: datetime) -> None:
    """복약 건 만들기, 시각이 된 건 열기, 기한 넘긴 건 인계 (backend.md 9장).

    1분 예약 작업(app/jobs.py)만 부른다. 다른 요청이 잠근 건은 건너뛰고 다음 주기에 처리한다.
    """
    sync_events(db, family.id, now)
    events = db.scalars(
        select(DoseEvent)
        .where(
            DoseEvent.family_id == family.id,
            DoseEvent.state.in_(("scheduled", "reading", *OPEN_STATES)),
        )
        .with_for_update(skip_locked=True)
    ).all()
    for e in events:
        advance_event(db, settings, push, family, e, now)
    db.flush()


def advance_event(
    db: Session, settings: Settings, push: PushSender, family: Family, e: DoseEvent, now: datetime
) -> None:
    """시각·기한에 따라 한 복약 건의 상태를 진행한다. 호출 전에 행을 잠근다."""
    if e.state == "reading":
        # 판독 중 서버가 죽은 경우: 판독 제한 시간이 지나면 다시 물어보는 상태로 돌린다
        last = db.scalar(
            select(Photo.uploaded_at)
            .where(Photo.dose_event_id == e.id)
            .order_by(Photo.uploaded_at.desc())
            .limit(1)
        )
        if last is None or now - last > timedelta(seconds=settings.reader_timeout_s * 2):
            e.state = "asked"
    if e.state == "scheduled" and e.scheduled_at <= now:
        e.state = "asked"
    if e.state == "asked" and now >= handoff_deadline(settings, e):
        create_handoff(db, push, family, e, "no_response", now)
    elif e.state in ("retake_requested", "reminded") and e.retake_deadline_at <= now:
        reason = "unclear" if e.state == "retake_requested" else "unchanged"
        create_handoff(db, push, family, e, reason, now)


@dataclass
class UploadOutcome:
    result: str  # recorded | retake | remind_retake | guardian_notified
    is_mock: bool


def can_accept_photo(db: Session, settings: Settings, event: DoseEvent, now: datetime) -> bool:
    if event.state in OPEN_STATES:
        return True
    if event.state == "scheduled":
        return now >= event.scheduled_at - timedelta(minutes=settings.early_photo_window_min)
    if event.state == "handed_off":
        h = open_bound_handoff(db, event.id)
        return h is not None and h.reason == "no_response"
    return False


def process_photo(
    db: Session,
    settings: Settings,
    push: PushSender,
    reader: Reader,
    family: Family,
    event: DoseEvent,
    photo: Photo,
    data: bytes,
    now: datetime,
    prior_state: str,
) -> UploadOutcome:
    """사진 판독과 분류. 호출 전에 can_accept_photo 로 확인한다.

    prior_state: 판독 중(reading) 표시 전의 상태. 판독하는 동안 event.state 는 reading 이다.
    """
    event.state = prior_state
    late = now > handoff_deadline(settings, event)
    late_handoff = open_bound_handoff(db, event.id) if event.state == "handed_off" else None
    if event.state == "scheduled":
        event.state = "asked"  # 미리 드시고 찍은 경우 (R-010 13번)

    def handoff(reason: str, reading: Reading | None, *, push_it: bool = True) -> UploadOutcome:
        if late_handoff is not None:
            # 이미 무응답으로 인계된 건: 사진만 붙이고 다시 알리지 않는다
            late_handoff.late_photo = True
        else:
            create_handoff(db, push, family, event, reason, now, reading=reading, send_push=push_it)
        return UploadOutcome("guardian_notified", is_mock)

    # 판독 한도 초과: 판독하지 않고 인계 (backend.md 6장)
    if rs.over_daily_limit(db, settings, family.id):
        is_mock = False
        return handoff("unclear", None)

    slot = db.get(ScheduleSlot, event.slot_id)
    pillbox = active_pillbox(db, family.id)
    refill = latest_refill(db, pillbox.id) if pillbox else None
    if pillbox is None or refill is None or slot.pillbox_id != pillbox.id:
        # 약통이 바뀌어 비교할 기준이 없음 (원칙 5)
        is_mock = False
        return handoff("unclear", None)

    layout = Layout.model_validate(pillbox.layout)
    reading, result = rs.read_cells(db, reader, photo, data, layout, dose_event_id=event.id)
    reading.target_cell_id = slot.cell_id
    is_mock = reading.is_mock
    db.flush()

    if result is None:
        reading.classification = "error"
        kind = "unclear"
        c = None
    else:
        expected = expected_states(db, refill, before=event.scheduled_at, exclude_event_id=event.id)
        c = classify(result, expected, slot.cell_id, settings.normal_confidence)
        reading.cells = c.cells
        kind = c.kind
        reading.classification = "unclear" if kind == "already_used" else kind
        if c.refill_suspected and kind != "already_used":
            _maybe_refill_check(db, push, family, event, refill, reading, now)

    if kind == "already_used":
        # R-014: 이번 주기에 이미 쓴 칸. 정상으로 기록하지 않는다
        if late_handoff is not None:
            late_handoff.late_photo = True
            return UploadOutcome("guardian_notified", is_mock)
        if c.refill_suspected:
            pending = _open_refill_check(db, family.id, refill)
            create_handoff(
                db,
                push,
                family,
                event,
                "refill_unconfirmed",
                now,
                reading=reading,
                send_push=pending is None,
            )
        else:
            create_handoff(db, push, family, event, "refill_overdue", now, reading=reading)
        return UploadOutcome("guardian_notified", is_mock)

    if kind == "normal":
        if late_handoff is not None:
            late_handoff.late_photo = True  # 정상이어도 인계는 열어 둔다 (R-010 4번)
        else:
            event.state = "done"
            event.outcome = "taken"
            event.outcome_source = "ai"
            event.taken_at = photo.taken_at
            event.resolved_at = now
            event.retake_deadline_at = None
        return UploadOutcome("recorded", is_mock)

    if kind == "double_suspected":
        return handoff("double_suspected", reading)

    if kind == "unclear":
        if late or late_handoff is not None or event.retake_count >= 1:
            return handoff("unclear", reading)
        event.state = "retake_requested"
        event.retake_count += 1
        event.retake_deadline_at = now + timedelta(minutes=settings.retake_wait_min)
        return UploadOutcome("retake", is_mock)

    # unchanged (칸 그대로): R-009 설정, C3(+30분 이내에만 안내)
    if (
        family.unchanged_mode == "remind_then_retake"
        and event.reminded_at is None
        and not late
        and late_handoff is None
    ):
        event.state = "reminded"
        event.reminded_at = now
        event.retake_deadline_at = now + timedelta(minutes=settings.retake_wait_min)
        return UploadOutcome("remind_retake", is_mock)
    return handoff("unchanged", reading)


def _open_refill_check(db: Session, family_id: str, refill: Refill) -> Handoff | None:
    return db.scalar(
        select(Handoff)
        .where(
            Handoff.family_id == family_id,
            Handoff.reason == "refill_unconfirmed",
            Handoff.verdict.is_(None),
            Handoff.notified_at >= refill.created_at,
        )
        .limit(1)
    )


def _maybe_refill_check(
    db: Session,
    push: PushSender,
    family: Family,
    event: DoseEvent,
    refill: Refill,
    reading: Reading,
    now: datetime,
) -> None:
    """리필 미기록 의심 확인 요청 1회 (C1). 열려 있으면 다시 만들지 않는다 (C2)."""
    if _open_refill_check(db, family.id, refill) is not None:
        return
    create_handoff(
        db, push, family, event, "refill_unconfirmed", now, reading=reading, binds_event=False
    )


def new_baseline_from_reading(
    db: Session, family_id: str, reading: Reading, device_id: str, pillbox_id: str
) -> Refill:
    """리필 확인 "네": 그 사진을 새 기준선으로 (source refill_check)."""
    photo = db.get(Photo, reading.photo_id)
    refill = Refill(
        family_id=family_id,
        pillbox_id=pillbox_id,
        filled_at=photo.taken_at,
        filled_by_device_id=device_id,
        photo_id=photo.id,
        baseline_reading_id=reading.id,
        baseline_cells=[
            {
                "cell_id": c["cell_id"],
                "state": "empty" if c["ai_state"] == "empty" else "full",
                "edited": False,
            }
            for c in reading.cells
        ],
        ignored_cells=[],
        source="refill_check",
    )
    db.add(refill)
    return refill
