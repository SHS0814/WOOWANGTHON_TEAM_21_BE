"""DB 객체 → API 응답 (api.md 2장 공통 객체)."""

from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device, DoseEvent, Handoff, Photo, Pillbox, Reading, Refill, ScheduleSlot
from app.storage import photo_out
from app.timeutil import iso


def base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def device_out(device: Device, me_id: str | None = None) -> dict:
    return {
        "id": device.id,
        "role": device.role,
        "display_name": device.display_name,
        "platform": device.platform,
        "paired_at": iso(device.paired_at),
        "consent_at": iso(device.consent_at),
        "last_seen_at": iso(device.last_seen_at),
        "is_me": device.id == me_id,
        "notification_status": notification_status_out(device),
    }


def notification_ready(device: Device) -> bool:
    return bool(device.notifications_allowed) and device.exact_alarm_allowed is not False


def notification_status_out(device: Device) -> dict | None:
    if device.notification_status_at is None:
        return None
    return {
        "notifications_allowed": device.notifications_allowed,
        "exact_alarm_allowed": device.exact_alarm_allowed,
        "ready": notification_ready(device),
        "reported_at": iso(device.notification_status_at),
    }


def elder_notification_warning(db: Session, family_id: str) -> bool:
    """보고한 어르신 기기 중 알림이 준비되지 않은 것이 있으면 True (R-032)."""
    elders = db.scalars(
        select(Device).where(
            Device.family_id == family_id,
            Device.role == "elder",
            Device.revoked_at.is_(None),
            Device.notification_status_at.is_not(None),
        )
    )
    return any(not notification_ready(d) for d in elders)


def person(device: Device | None) -> dict | None:
    return None if device is None else {"device_id": device.id, "display_name": device.display_name}


def active_guardians(db: Session, family_id: str) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(
                Device.family_id == family_id,
                Device.role == "guardian",
                Device.revoked_at.is_(None),
            )
            .order_by(Device.paired_at)
        )
    )


def slot_out(slot: ScheduleSlot) -> dict:
    return {
        "id": slot.id,
        "weekday": slot.weekday,
        "time_of_day": slot.time_of_day,
        "dose_time": slot.dose_time,
        "cell_id": slot.cell_id,
    }


def pillbox_out(pb: Pillbox) -> dict:
    return {
        "id": pb.id,
        "layout": pb.layout,
        "registered_photo_id": pb.registered_photo_id,
        "confirmed_at": iso(pb.confirmed_at),
        "active": pb.active,
    }


def reading_out(db: Session, settings: Settings, url_base: str, reading: Reading) -> dict:
    photo = db.get(Photo, reading.photo_id)
    return {
        "id": reading.id,
        "photo": photo_out(settings, url_base, photo),
        "is_mock": reading.is_mock,
        "provider": reading.provider,
        "model": reading.model,
        "created_at": iso(reading.created_at),
        "target_cell_id": reading.target_cell_id,
        "classification": reading.classification,
        "cells": reading.cells,
    }


def refill_out(db: Session, settings: Settings, url_base: str, refill: Refill) -> dict:
    filled_by = db.get(Device, refill.filled_by_device_id)
    reading = db.get(Reading, refill.baseline_reading_id)
    unclear = {c["cell_id"] for c in reading.cells if c["ai_state"] == "unclear"}
    needs_review = any(
        c["cell_id"] in unclear and not c.get("edited") for c in refill.baseline_cells
    )
    return {
        "id": refill.id,
        "pillbox_id": refill.pillbox_id,
        "filled_at": iso(refill.filled_at),
        "filled_by": person(filled_by),
        "source": refill.source,
        "baseline_reading": reading_out(db, settings, url_base, reading),
        "baseline_cells": refill.baseline_cells,
        "needs_review": needs_review,
    }


# ---------- 복약 건, 인계 (api.md 2.5, 2.6) ----------


def _elder_status(event) -> str:
    if event.state == "scheduled":
        return "upcoming"
    if event.state in ("asked", "reading", "retake_requested", "reminded"):
        return "waiting"
    if event.outcome == "taken":
        return "recorded"
    # 인계 뒤 늦게 온 정상 사진: 어르신 기준으로는 기록됨 (R-021 Q-004)
    if event.outcome in (None, "acknowledged") and event.taken_at is not None:
        return "recorded"
    return "no_record"


def handoff_brief(h) -> dict | None:
    if h is None:
        return None
    return {
        "id": h.id,
        "reason": h.reason,
        "notified_at": iso(h.notified_at),
        "verdict": h.verdict,
        "verdict_at": iso(h.verdict_at),
    }


def event_out(
    db: Session,
    settings: Settings,
    url_base: str,
    event: DoseEvent,
    *,
    guardian: bool,
    carried_over: bool = False,
) -> dict:
    slot = db.get(ScheduleSlot, event.slot_id)
    photos = db.scalars(
        select(Photo)
        .where(Photo.dose_event_id == event.id, Photo.deleted_at.is_(None))
        .order_by(Photo.uploaded_at)
    ).all()
    readings = db.scalars(
        select(Reading)
        .where(Reading.dose_event_id == event.id, Reading.kind == "cells")
        .order_by(Reading.created_at)
    ).all()
    handoff = None
    if guardian:
        handoff = db.scalar(
            select(Handoff)
            .where(Handoff.dose_event_id == event.id, Handoff.binds_event)
            .order_by(Handoff.notified_at.desc())
            .limit(1)
        )
    decided_by = db.get(Device, event.decided_by_device_id) if event.decided_by_device_id else None
    return _event_dict(
        db,
        settings,
        url_base,
        event,
        slot,
        photos,
        readings,
        handoff,
        decided_by,
        guardian=guardian,
        carried_over=carried_over,
    )


def events_out(
    db: Session, settings: Settings, url_base: str, events: list[DoseEvent], *, guardian: bool
) -> list[dict]:
    """여러 복약 건을 한꺼번에 직렬화한다 (기간 기록 366일, R-051). 결과는 event_out 과 같다.

    건마다 쿼리를 날리지 않고 칸·사진·판독·인계·기기를 종류별로 한 번씩 읽는다.
    """
    if not events:
        return []
    ids = [e.id for e in events]
    photos: dict[str, list[Photo]] = {i: [] for i in ids}
    readings: dict[str, list[Reading]] = {i: [] for i in ids}
    handoffs: dict[str, Handoff] = {}
    # 이 목록들이 세션 identity map 의 객체를 붙잡아 두어 아래 db.get 이 쿼리 없이 끝난다
    keep: list = list(
        db.scalars(select(ScheduleSlot).where(ScheduleSlot.id.in_({e.slot_id for e in events})))
    )
    for chunk in _chunks(ids):
        for p in db.scalars(
            select(Photo)
            .where(Photo.dose_event_id.in_(chunk), Photo.deleted_at.is_(None))
            .order_by(Photo.uploaded_at)
        ):
            photos[p.dose_event_id].append(p)
        for r in db.scalars(
            select(Reading)
            .where(Reading.dose_event_id.in_(chunk), Reading.kind == "cells")
            .order_by(Reading.created_at)
        ):
            readings[r.dose_event_id].append(r)
        if guardian:
            for h in db.scalars(
                select(Handoff)
                .where(Handoff.dose_event_id.in_(chunk), Handoff.binds_event)
                .order_by(Handoff.notified_at)
            ):
                handoffs[h.dose_event_id] = h  # 마지막(최신)이 남는다
    device_ids = {e.decided_by_device_id for e in events if e.decided_by_device_id}
    if device_ids:
        keep += db.scalars(select(Device).where(Device.id.in_(device_ids))).all()
    if guardian:
        # latest_reading 의 사진 (보관 기한으로 지워진 사진 포함)
        photo_ids = {lr.photo_id for rs in readings.values() if (lr := _latest(rs)) is not None}
        if photo_ids:
            keep += db.scalars(select(Photo).where(Photo.id.in_(photo_ids))).all()
    return [
        _event_dict(
            db,
            settings,
            url_base,
            e,
            db.get(ScheduleSlot, e.slot_id),
            photos[e.id],
            readings[e.id],
            handoffs.get(e.id),
            db.get(Device, e.decided_by_device_id) if e.decided_by_device_id else None,
            guardian=guardian,
            carried_over=False,
        )
        for e in events
    ]


def _chunks(ids: list[str], size: int = 500):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _latest(readings) -> Reading | None:
    return next((r for r in reversed(readings) if r.error is None and r.cells), None)


def _double_photo(db: Session, event: DoseEvent, handoff: Handoff | None) -> list:
    """중복 복용 판정으로 건너뛴 건(자기 사진이 없음)에는 그 판정의 사진을 함께 보여 준다.

    건너뛴 건은 인계 없이 보호자가 acknowledged 로 닫은 건이고, 닫힌 시각이 판정 시각과 같다
    (routers/handoffs._close_pre_emptied). 어르신의 "나 약 먹었나?"에 두 건이 같은 사진으로 나온다.
    """
    if (
        handoff is not None
        or event.outcome != "acknowledged"
        or event.outcome_source != "guardian"
        or event.resolved_at is None
    ):
        return []
    h = db.scalar(
        select(Handoff)
        .where(
            Handoff.family_id == event.family_id,
            Handoff.reason == "double_suspected",
            Handoff.verdict == "acknowledged",
            Handoff.verdict_at == event.resolved_at,
        )
        .limit(1)
    )
    reading = db.get(Reading, h.reading_id) if h is not None and h.reading_id else None
    photo = db.get(Photo, reading.photo_id) if reading is not None else None
    return [photo] if photo is not None else []


def _event_dict(
    db: Session,
    settings: Settings,
    url_base: str,
    event: DoseEvent,
    slot: ScheduleSlot,
    photos,
    readings,
    handoff: Handoff | None,
    decided_by: Device | None,
    *,
    guardian: bool,
    carried_over: bool,
) -> dict:
    latest = _latest(readings)
    if not photos:
        photos = _double_photo(db, event, handoff)
    asks = [event.scheduled_at] + [
        event.scheduled_at + timedelta(minutes=m) for m in settings.reask_offsets_min
    ]
    return {
        "id": event.id,
        "date": event.date,
        "slot": slot_out(slot),
        "scheduled_at": iso(event.scheduled_at),
        "state": event.state,
        "outcome": event.outcome,
        "outcome_source": event.outcome_source,
        "decided_by": person(decided_by),
        "taken_at": iso(event.taken_at),
        "resolved_at": iso(event.resolved_at),
        "elder_status": _elder_status(event),
        "ask_times": [iso(t) for t in asks],
        "handoff_at": iso(event.scheduled_at + timedelta(minutes=settings.no_response_handoff_min)),
        "retake_deadline_at": iso(event.retake_deadline_at)
        if event.state in ("retake_requested", "reminded")
        else None,
        "photos": [photo_out(settings, url_base, p) for p in photos],
        "is_mock": any(r.is_mock for r in readings),
        "is_simulated": event.is_simulated,
        "carried_over": carried_over,
        "reminded_at": iso(event.reminded_at) if guardian else None,
        "latest_reading": reading_out(db, settings, url_base, latest)
        if guardian and latest
        else None,
        "handoff": handoff_brief(handoff) if guardian else None,
    }


def handoff_out(
    db: Session, settings: Settings, url_base: str, h: Handoff, *, detail: bool
) -> dict:
    from app.services.dose import ALLOWED_VERDICTS

    event = db.get(DoseEvent, h.dose_event_id)
    verdict_by = db.get(Device, h.verdict_by_device_id) if h.verdict_by_device_id else None
    out = {
        "id": h.id,
        "reason": h.reason,
        "notified_at": iso(h.notified_at),
        "verdict": h.verdict,
        "verdict_by": person(verdict_by),
        "verdict_at": iso(h.verdict_at),
        "allowed_verdicts": ALLOWED_VERDICTS[h.reason],
        "late_photo": h.late_photo,
        "dose_event": event_out(db, settings, url_base, event, guardian=True),
    }
    if detail:
        readings = db.scalars(
            select(Reading)
            .where(
                Reading.dose_event_id == event.id,
                Reading.kind == "cells",
                Reading.error.is_(None),
            )
            .order_by(Reading.created_at)
        ).all()
        slot = db.get(ScheduleSlot, event.slot_id)
        pb = db.get(Pillbox, slot.pillbox_id)
        out["readings"] = [reading_out(db, settings, url_base, r) for r in readings]
        out["layout"] = pb.layout
    return out
