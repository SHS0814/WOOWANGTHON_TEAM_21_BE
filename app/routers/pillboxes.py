"""약통 등록, 일정, 리필 (api.md 3.10~3.16, backend.md 4.1·5.1)."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_any, auth_guardian, get_db, get_settings
from app.errors import ApiError, not_found, validation
from app.models import DoseEvent, Photo, Pillbox, Refill, ScheduleSlot
from app.reader import Layout
from app.serializers import base_url, pillbox_out, refill_out, slot_out
from app.services import readings as rs
from app.services.expected import (
    active_pillbox,
    active_slots,
    latest_refill,
    pillbox_mismatch,
    refill_status,
)
from app.storage import load, photo_out
from app.timeutil import iso, kst_date

router = APIRouter()

TimeOfDay = Literal["morning", "noon", "evening", "bedtime"]


def _require_pillbox(db: Session, family_id: str, status: int = 409) -> Pillbox:
    pb = active_pillbox(db, family_id)
    if pb is None:
        raise ApiError(status, "pillbox_required", "약통을 먼저 등록해야 함")
    return pb


async def _read_upload(photo: UploadFile, settings: Settings) -> bytes:
    data = await photo.read(settings.photo_max_bytes + 1)
    if len(data) > settings.photo_max_bytes:
        raise ApiError(413, "photo_too_large", "사진이 너무 큼")
    return data


# ---------- 약통 ----------


@router.post("/pillboxes/detect")
async def detect_pillbox(
    request: Request,
    photo: Annotated[UploadFile, File()],
    taken_at: Annotated[datetime | None, Form()] = None,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    data = await _read_upload(photo, settings)
    rs.ensure_daily_limit(db, settings, auth.family.id)
    p = rs.store_photo(
        db,
        settings,
        family_id=auth.family.id,
        device_id=auth.device.id,
        data=data,
        taken_at=taken_at,
    )
    _, proposal = rs.read_layout(db, request.app.state.layout_reader, p, data)
    db.commit()
    return {
        "photo": photo_out(settings, base_url(request, settings), p),
        "proposal": {
            "layout": proposal.layout.model_dump(),
            "suggested_time_of_day": proposal.suggested_time_of_day,
            "uncertain": proposal.uncertain,
        },
        "is_mock": proposal.is_mock,
        "provider": proposal.provider,
        "model": proposal.model,
    }


class PillboxIn(BaseModel):
    photo_id: str
    layout: dict


def _parse_layout(raw: dict) -> Layout:
    try:
        layout = Layout.model_validate(raw)
    except ValidationError as e:
        raise validation("layout", e.errors()[0]["msg"]) from e
    ids = layout.cell_ids()
    if len(ids) != len(set(ids)):
        raise validation("layout.cells", "칸 id 중복")
    if len(layout.row_labels) not in (0, layout.rows):
        raise validation("layout.row_labels", "길이는 rows 와 같아야 함")
    if len(layout.col_labels) not in (0, layout.cols):
        raise validation("layout.col_labels", "길이는 cols 와 같아야 함")
    return layout


def _baseline_refill(
    db: Session,
    settings: Settings,
    request: Request,
    *,
    auth: Auth,
    pillbox_id: str,
    photo: Photo,
    layout: Layout,
    source: str,
) -> Refill:
    """사진을 칸 판독해서 기준선을 만든다. 애매한 칸은 일단 full (api.md 3.15)."""
    data = load(settings, photo)
    reading, result = rs.read_cells(
        db, request.app.state.cells_reader, photo, data, layout, purpose="baseline"
    )
    if result is None:
        db.commit()  # 실패 기록은 남긴다
        raise ApiError(502, "reading_failed", "AI 판독 실패")
    reading.cells = rs.baseline_cell_results(result, settings)
    db.flush()
    refill = Refill(
        family_id=auth.family.id,
        pillbox_id=pillbox_id,
        filled_at=photo.taken_at,
        filled_by_device_id=auth.device.id,
        photo_id=photo.id,
        baseline_reading_id=reading.id,
        baseline_cells=[
            {
                "cell_id": c.cell_id,
                "state": "empty" if c.state == "empty" else "full",
                "edited": False,
            }
            for c in result.cells
        ],
        ignored_cells=[],
        source=source,
    )
    db.add(refill)
    return refill


def _retire_slots(db: Session, family_id: str, now: datetime) -> None:
    """기존 일정을 끄고, 아직 시작 안 한 오늘·이후 복약 건을 지운다."""
    old = active_slots(db, family_id)
    for s in old:
        s.active = False
    if old:
        db.execute(
            delete(DoseEvent).where(
                DoseEvent.slot_id.in_([s.id for s in old]),
                DoseEvent.state == "scheduled",
                DoseEvent.date >= kst_date(now).isoformat(),
            )
        )


@router.post("/pillboxes", status_code=201)
def create_pillbox(
    body: PillboxIn,
    request: Request,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    layout = _parse_layout(body.layout)
    photo = db.get(Photo, body.photo_id)
    if photo is None or photo.family_id != auth.family.id or photo.deleted_at is not None:
        raise not_found("사진")
    rs.ensure_daily_limit(db, settings, auth.family.id)

    now = clock.now()
    old = active_pillbox(db, auth.family.id)
    pb = Pillbox(
        family_id=auth.family.id,
        layout=layout.model_dump(),
        registered_photo_id=photo.id,
        confirmed_at=now,
    )
    db.add(pb)
    db.flush()
    refill = _baseline_refill(
        db,
        settings,
        request,
        auth=auth,
        pillbox_id=pb.id,
        photo=photo,
        layout=layout,
        source="registration",
    )
    if old is not None:
        old.active = False
    _retire_slots(db, auth.family.id, now)
    db.commit()
    return {
        "pillbox": pillbox_out(pb),
        "refill": refill_out(db, settings, base_url(request, settings), refill),
    }


@router.get("/pillboxes/current")
def current_pillbox(
    request: Request,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    pb = _require_pillbox(db, auth.family.id, status=404)
    refill = latest_refill(db, pb.id)
    return {
        "pillbox": pillbox_out(pb),
        "last_refill": refill_out(db, settings, base_url(request, settings), refill)
        if refill
        else None,
        "refill_status": refill_status(db, settings, auth.family.id, refill, clock.now()).out(),
        "pillbox_mismatch": pillbox_mismatch(db, refill),
    }


# ---------- 일정 ----------


def _schedule_out(db: Session, family_id: str) -> dict:
    pb = active_pillbox(db, family_id)
    slots = active_slots(db, family_id)
    return {
        "pillbox_id": pb.id if pb else None,
        "slots": [slot_out(s) for s in slots],
        "updated_at": iso(max((s.created_at for s in slots), default=None)),
    }


@router.get("/schedule")
def get_schedule(auth: Auth = Depends(auth_any), db: Session = Depends(get_db)):
    return _schedule_out(db, auth.family.id)


class SlotIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    time_of_day: TimeOfDay
    dose_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    cell_id: str = Field(min_length=1, max_length=16)


class ScheduleIn(BaseModel):
    slots: list[SlotIn] = Field(max_length=49)


@router.put("/schedule")
def put_schedule(
    body: ScheduleIn, auth: Auth = Depends(auth_guardian), db: Session = Depends(get_db)
):
    pb = _require_pillbox(db, auth.family.id)
    cell_ids = {c["id"] for c in pb.layout["cells"]}
    # 같은 칸을 여러 요일에 쓰는 하루 약통은 허용한다 (R-054). 같은 요일 안에서만 겹치면 안 된다
    seen_cells: set[tuple[str, int]] = set()
    seen_times: set[tuple[int, str]] = set()
    for i, s in enumerate(body.slots):
        if s.cell_id not in cell_ids:
            raise validation(f"slots.{i}.cell_id", "약통에 없는 칸")
        if (s.cell_id, s.weekday) in seen_cells:
            raise validation(f"slots.{i}.cell_id", "같은 요일에 같은 칸 두 번")
        if (s.weekday, s.time_of_day) in seen_times:
            raise validation(f"slots.{i}", "같은 요일·시간대 두 번")
        seen_cells.add((s.cell_id, s.weekday))
        seen_times.add((s.weekday, s.time_of_day))

    now = clock.now()
    _retire_slots(db, auth.family.id, now)
    for s in body.slots:
        db.add(ScheduleSlot(family_id=auth.family.id, pillbox_id=pb.id, **s.model_dump()))
    db.flush()
    from app.services.dose import sync_events

    sync_events(db, auth.family.id, now)
    db.commit()
    return _schedule_out(db, auth.family.id)


# ---------- 리필 ----------


@router.post("/refills", status_code=201)
async def create_refill(
    request: Request,
    photo: Annotated[UploadFile, File()],
    taken_at: Annotated[datetime | None, Form()] = None,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    pb = _require_pillbox(db, auth.family.id)
    data = await _read_upload(photo, settings)
    rs.ensure_daily_limit(db, settings, auth.family.id)
    p = rs.store_photo(
        db,
        settings,
        family_id=auth.family.id,
        device_id=auth.device.id,
        data=data,
        taken_at=taken_at,
    )
    refill = _baseline_refill(
        db,
        settings,
        request,
        auth=auth,
        pillbox_id=pb.id,
        photo=p,
        layout=Layout.model_validate(pb.layout),
        source="guardian_photo",
    )
    db.commit()
    return refill_out(db, settings, base_url(request, settings), refill)


class BaselineCellIn(BaseModel):
    cell_id: str
    state: Literal["full", "empty"]


class BaselineIn(BaseModel):
    cells: list[BaselineCellIn] = Field(min_length=1)


@router.put("/refills/{refill_id}/baseline")
def put_baseline(
    refill_id: str,
    body: BaselineIn,
    request: Request,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    refill = db.get(Refill, refill_id)
    if refill is None or refill.family_id != auth.family.id:
        raise not_found("리필")
    latest = latest_refill(db, refill.pillbox_id)
    pb = active_pillbox(db, auth.family.id)
    if latest is None or latest.id != refill.id or pb is None or pb.id != refill.pillbox_id:
        raise ApiError(409, "refill_not_latest", "가장 최근 리필만 고칠 수 있음")
    cells = [dict(c) for c in refill.baseline_cells]
    index = {c["cell_id"]: c for c in cells}
    for i, c in enumerate(body.cells):
        if c.cell_id not in index:
            raise validation(f"cells.{i}.cell_id", "약통에 없는 칸")
        index[c.cell_id].update(state=c.state, edited=True)
    refill.baseline_cells = cells
    db.commit()
    return refill_out(db, settings, base_url(request, settings), refill)
