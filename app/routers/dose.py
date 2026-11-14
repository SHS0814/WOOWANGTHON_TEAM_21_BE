"""복약 건, 사진 업로드, 시연 트리거 (api.md 3.17~3.19, 3.27)."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_any, auth_elder, auth_guardian, get_db, get_settings
from app.errors import ApiError, not_found, validation
from app.models import DoseEvent, Handoff, Photo, Reading, Refill, ScheduleSlot
from app.serializers import base_url, elder_notification_warning, event_out, events_out
from app.services import dose as ds
from app.services import readings as rs
from app.services.expected import active_pillbox, latest_refill, refill_status
from app.storage import full_path, load
from app.timeutil import iso, kst_date

router = APIRouter()


def _events_for(
    db: Session,
    family_id: str,
    start: date,
    end: date,
    *,
    filter_: str | None = None,
    reason: str | None = None,
    desc: bool = False,
) -> list[DoseEvent]:
    q = select(DoseEvent).where(
        DoseEvent.family_id == family_id,
        DoseEvent.date >= start.isoformat(),
        DoseEvent.date <= end.isoformat(),
    )
    # 인계가 있었던 건: 사유·판정과 관계없이 인계 행이 하나라도 있는 건 (R-051)
    had_handoff = select(Handoff.id).where(Handoff.dose_event_id == DoseEvent.id).exists()
    if filter_ == "exceptions":
        q = q.where(or_(had_handoff, DoseEvent.outcome == "not_taken"))
    elif filter_ == "handoff":
        q = q.where(had_handoff)
    elif filter_ in ("not_taken", "taken"):
        q = q.where(DoseEvent.outcome == filter_)
    if reason is not None:
        q = q.where(
            select(Handoff.id)
            .where(Handoff.dose_event_id == DoseEvent.id, Handoff.reason == reason)
            .exists()
        )
    order = DoseEvent.scheduled_at.desc() if desc else DoseEvent.scheduled_at
    return list(db.scalars(q.order_by(order)))


@router.get("/dose-events/today")
def today_events(
    request: Request,
    auth: Auth = Depends(auth_any),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # 상태 갱신은 1분 예약 작업이 한다. GET 은 읽기만 한다 (R-018)
    now = clock.now()
    today = kst_date(now)
    events = _events_for(db, auth.family.id, today, today)
    # 전날 끝나지 않은 건을 앞쪽에 함께 보여준다 (R-021 Q-007)
    carried = ds.carried_over_events(db, auth.family.id, now)
    url = base_url(request, settings)
    next_event = next((e for e in events if e.state in ("scheduled", "asked")), None)
    open_count = None
    if auth.is_guardian:
        open_count = db.scalar(
            select(func.count(Handoff.id)).where(
                Handoff.family_id == auth.family.id, Handoff.verdict.is_(None)
            )
        )
    return {
        "date": today.isoformat(),
        "items": [
            event_out(db, settings, url, e, guardian=auth.is_guardian, carried_over=True)
            for e in carried
        ]
        + [event_out(db, settings, url, e, guardian=auth.is_guardian) for e in events],
        "open_handoff_count": open_count,
        "elder_notification_warning": elder_notification_warning(db, auth.family.id)
        if auth.is_guardian
        else None,
        "next_event": None
        if next_event is None
        else {
            "id": next_event.id,
            "scheduled_at": iso(next_event.scheduled_at),
            "time_of_day": db.get(ScheduleSlot, next_event.slot_id).time_of_day,
        },
    }


# api.md HandoffReason (ds.ALLOWED_VERDICTS 의 키와 같다. 테스트로 확인)
HandoffReason = Literal[
    "no_response",
    "unclear",
    "unchanged",
    "double_suspected",
    "refill_unconfirmed",
    "refill_overdue",
]


@router.get("/dose-events")
def list_events(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: date,
    filter_: Annotated[
        Literal["exceptions", "handoff", "not_taken", "taken"] | None, Query(alias="filter")
    ] = None,
    reason: HandoffReason | None = None,
    order: Literal["asc", "desc"] = "asc",
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if from_ > to:
        raise validation("from", "from 이 to 보다 늦음")
    # 검색(filter·reason)이면 최대 366일, 아니면 31일 (R-051)
    limit = 366 if filter_ is not None or reason is not None else 31
    if (to - from_).days >= limit:
        raise validation("to", f"최대 {limit}일")
    url = base_url(request, settings)
    events = _events_for(
        db, auth.family.id, from_, to, filter_=filter_, reason=reason, desc=order == "desc"
    )
    return {
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "items": events_out(db, settings, url, events, guardian=True),
    }


# async def 로 두지 않는다. 판독이 이벤트 루프를 막아 그동안 서버 전체가 멈춘다 (pillboxes.py 의 설명 참고)
@router.post("/dose-events/{event_id}/photo")
def upload_dose_photo(
    event_id: str,
    request: Request,
    photo: Annotated[UploadFile, File()],
    taken_at: Annotated[datetime | None, Form()] = None,
    client_upload_id: Annotated[str | None, Form(max_length=64)] = None,
    auth: Auth = Depends(auth_elder),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    event = db.get(DoseEvent, event_id)
    if event is None or event.family_id != auth.family.id:
        raise not_found("복약 건")
    url = base_url(request, settings)

    if client_upload_id:
        prior = db.scalar(
            select(Photo).where(
                Photo.dose_event_id == event.id, Photo.client_upload_id == client_upload_id
            )
        )
        if prior is not None and prior.upload_result is not None:
            return {
                "result": prior.upload_result,
                "dose_event": event_out(db, settings, url, event, guardian=False),
                "is_mock": prior.upload_is_mock,
                "refill_prompt": bool(prior.upload_refill_prompt),
            }

    data = photo.file.read(settings.photo_max_bytes + 1)
    if len(data) > settings.photo_max_bytes:
        raise ApiError(413, "photo_too_large", "사진이 너무 큼")
    now = clock.now()
    # 이 건만 잠그고 시각·기한을 반영한다. 같은 건의 동시 업로드·예약 작업과 겹치지 않는다
    event = ds.lock_event(db, event.id)
    ds.advance_event(db, settings, request.app.state.push, auth.family, event, now)
    if event.state == "reading":
        raise ApiError(409, "reading_in_progress", "판독 중")
    if not ds.can_accept_photo(db, settings, event, now):
        raise ApiError(409, "event_not_open", "사진을 받을 수 없는 상태")

    p = rs.store_photo(
        db,
        settings,
        family_id=auth.family.id,
        device_id=auth.device.id,
        data=data,
        taken_at=taken_at,
        dose_event_id=event.id,
        client_upload_id=client_upload_id,
    )
    scenario = request.app.state.mock_scenarios.pop(auth.family.id, None)
    prev_state = event.state
    event.state = "reading"
    db.commit()
    try:
        outcome = ds.process_photo(
            db,
            settings,
            request.app.state.push,
            request.app.state.cells_reader,
            auth.family,
            event,
            p,
            load(settings, p),
            now,
            prev_state,
            mock_scenario=scenario,
        )
    except Exception:
        db.rollback()
        event.state = prev_state
        db.commit()
        raise
    p.upload_result = outcome.result
    p.upload_is_mock = outcome.is_mock
    p.upload_refill_prompt = _refill_prompt(db, settings, auth.family, outcome.result, now)
    db.commit()
    return {
        "result": outcome.result,
        "dose_event": event_out(db, settings, url, event, guardian=False),
        "is_mock": outcome.is_mock,
        "refill_prompt": p.upload_refill_prompt,
    }


def _refill_prompt(db: Session, settings: Settings, family, result: str, now: datetime) -> bool:
    """정상 기록 뒤 약통 남은 칸이 0이면 어르신에게 채워 달라고 한다 (R-071, api.md 3.18).

    남은 칸은 GET /pillboxes/current 의 remaining_cells 와 같은 계산(기대 상태의 full 칸 수).
    """
    if result != "recorded" or not family.elder_refill_prompt:
        return False
    pb = active_pillbox(db, family.id)
    refill = latest_refill(db, pb.id) if pb is not None else None
    if refill is None:
        return False
    return refill_status(db, settings, family.id, refill, now).remaining_cells == 0


class SimulateIn(BaseModel):
    slot_id: str


@router.post("/dev/dose-events", status_code=201)
def simulate_dose_event(
    body: SimulateIn,
    request: Request,
    auth: Auth = Depends(auth_any),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """시연 트리거 (R-010 10번). DEMO_MODE 와 데모 가족이 아니면 경로가 없는 것처럼 404."""
    if not (settings.demo_mode and auth.family.is_demo):
        raise not_found()
    slot = db.get(ScheduleSlot, body.slot_id)
    if slot is None or slot.family_id != auth.family.id or not slot.active:
        raise not_found("일정 칸")
    now = clock.now()
    today = kst_date(now)
    if slot.weekday != today.weekday():
        raise validation("slot_id", "오늘 요일의 칸이 아님")
    ds.sync_events(db, auth.family.id, now)
    event = db.scalar(
        select(DoseEvent).where(DoseEvent.slot_id == slot.id, DoseEvent.date == today.isoformat())
    )
    if event is None:
        # 오늘 그 시각이 지난 뒤에 저장한 칸은 오늘 건이 없다 (R-021 Q-001). 시연은 열 수 있게 만든다
        event = DoseEvent(
            family_id=auth.family.id,
            slot_id=slot.id,
            date=today.isoformat(),
            scheduled_at=now,
            state="scheduled",
        )
        db.add(event)
        db.flush()
    if event.state != "scheduled":
        raise ApiError(409, "event_already_started", "오늘 그 칸의 복약 건이 이미 진행됨")
    event.scheduled_at = now
    event.is_simulated = True
    event.state = "asked"
    db.commit()
    return event_out(db, settings, base_url(request, settings), event, guardian=auth.is_guardian)


@router.post("/dev/reset", status_code=204)
def reset_demo_records(
    request: Request,
    auth: Auth = Depends(auth_any),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """시연 초기화: 이 데모 가족의 복약 기록을 모두 지워 시연을 처음부터 다시 할 수 있게 한다.

    DEMO_MODE 와 데모 가족이 아니면 경로가 없는 것처럼 404. 지우는 것: 복약 건, 인계, 복약 사진과 그 판독,
    복약 사진으로 만든 기준선(자동 채움·리필 확인). 남는 것: 가족·기기 연결, 약통, 일정, 설정,
    약통 등록·보호자 채우기 때의 기준선. 그래서 약통은 마지막으로 등록하거나 채운 상태로 돌아간다.
    """
    if not (settings.demo_mode and auth.family.is_demo):
        raise not_found()
    fid = auth.family.id
    now = clock.now()
    dose_readings = select(Reading.id).where(
        Reading.family_id == fid, Reading.dose_event_id.is_not(None)
    )
    photos = list(
        db.scalars(select(Photo).where(Photo.family_id == fid, Photo.dose_event_id.is_not(None)))
    )
    db.execute(delete(Handoff).where(Handoff.family_id == fid))
    db.execute(
        delete(Refill).where(Refill.family_id == fid, Refill.baseline_reading_id.in_(dose_readings))
    )
    db.execute(delete(Reading).where(Reading.family_id == fid, Reading.dose_event_id.is_not(None)))
    db.execute(delete(Photo).where(Photo.family_id == fid, Photo.dose_event_id.is_not(None)))
    db.execute(delete(DoseEvent).where(DoseEvent.family_id == fid))
    # 오늘 이미 지난 시각의 건이 예약 작업으로 다시 생겨 무응답 인계가 되지 않게, 일정을 방금 저장한 것처럼 둔다
    # (sync_events: 그 시각이 지난 뒤에 저장한 칸은 오늘 건을 만들지 않는다). 지난 시간대는 시연 트리거로 연다
    db.execute(
        update(ScheduleSlot)
        .where(ScheduleSlot.family_id == fid, ScheduleSlot.active)
        .values(created_at=now)
    )
    request.app.state.mock_scenarios.pop(fid, None)
    db.commit()
    for p in photos:
        full_path(settings, p).unlink(missing_ok=True)
    return Response(status_code=204)


class MockScenarioIn(BaseModel):
    scenario: Literal["normal", "unclear", "unchanged", "double_suspected"] | None


@router.post("/dev/mock-scenario")
def set_mock_scenario(
    body: MockScenarioIn,
    request: Request,
    auth: Auth = Depends(auth_any),
    settings: Settings = Depends(get_settings),
):
    """QA용: 다음 복약 사진 1장의 모의 판독 결과를 정한다 (R-018).

    DEMO_MODE, 데모 가족, 매일 판독기가 mock 일 때만. 그 밖에는 경로가 없는 것처럼 404.
    """
    reader = request.app.state.cells_reader
    if not (settings.demo_mode and auth.family.is_demo and reader.provider == "mock"):
        raise not_found()
    scenarios = request.app.state.mock_scenarios
    if body.scenario is None:
        scenarios.pop(auth.family.id, None)
    else:
        scenarios[auth.family.id] = body.scenario
    return {"scenario": body.scenario, "applies_to": "next_dose_photo", "is_mock": True}
