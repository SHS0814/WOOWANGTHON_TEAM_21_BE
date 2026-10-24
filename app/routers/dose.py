"""복약 건, 사진 업로드, 시연 트리거 (api.md 3.17~3.19, 3.27)."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_any, auth_elder, auth_guardian, get_db, get_settings
from app.errors import ApiError, not_found, validation
from app.models import DoseEvent, Handoff, Photo, ScheduleSlot
from app.serializers import base_url, event_out
from app.services import dose as ds
from app.services import readings as rs
from app.storage import load
from app.timeutil import iso, kst_date

router = APIRouter()


def _events_for(db: Session, family_id: str, start: date, end: date) -> list[DoseEvent]:
    return list(
        db.scalars(
            select(DoseEvent)
            .where(
                DoseEvent.family_id == family_id,
                DoseEvent.date >= start.isoformat(),
                DoseEvent.date <= end.isoformat(),
            )
            .order_by(DoseEvent.scheduled_at)
        )
    )


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
        "items": [event_out(db, settings, url, e, guardian=auth.is_guardian) for e in events],
        "open_handoff_count": open_count,
        "next_event": None
        if next_event is None
        else {
            "id": next_event.id,
            "scheduled_at": iso(next_event.scheduled_at),
            "time_of_day": db.get(ScheduleSlot, next_event.slot_id).time_of_day,
        },
    }


@router.get("/dose-events")
def list_events(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: date,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if from_ > to:
        raise validation("from", "from 이 to 보다 늦음")
    if (to - from_).days >= 31:
        raise validation("to", "최대 31일")
    url = base_url(request, settings)
    events = _events_for(db, auth.family.id, from_, to)
    return {
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "items": [event_out(db, settings, url, e, guardian=True) for e in events],
    }


@router.post("/dose-events/{event_id}/photo")
async def upload_dose_photo(
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
            }

    data = await photo.read(settings.photo_max_bytes + 1)
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
    db.commit()
    return {
        "result": outcome.result,
        "dose_event": event_out(db, settings, url, event, guardian=False),
        "is_mock": outcome.is_mock,
    }


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
    if event.state != "scheduled":
        raise ApiError(409, "event_already_started", "오늘 그 칸의 복약 건이 이미 진행됨")
    event.scheduled_at = now
    event.is_simulated = True
    event.state = "asked"
    db.commit()
    return event_out(db, settings, base_url(request, settings), event, guardian=auth.is_guardian)


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
