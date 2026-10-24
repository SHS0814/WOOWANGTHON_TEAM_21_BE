"""사진 저장과 판독 호출. 판독 결과는 성공·실패 모두 readings 에 남긴다 (backend.md 8장)."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.errors import ApiError
from app.models import Photo, Reading
from app.reader import CellReadings, Layout, LayoutProposal, Reader, ReaderError
from app.storage import check_upload, save
from app.timeutil import KST, kst_date, kst_day_bounds


def readings_today(db: Session, family_id: str) -> int:
    start, end = kst_day_bounds(kst_date(clock.now()))
    return db.scalar(
        select(func.count(Reading.id)).where(
            Reading.family_id == family_id, Reading.created_at >= start, Reading.created_at < end
        )
    )


def over_daily_limit(db: Session, settings: Settings, family_id: str) -> bool:
    return readings_today(db, family_id) >= settings.daily_reading_limit


def ensure_daily_limit(db: Session, settings: Settings, family_id: str) -> None:
    if over_daily_limit(db, settings, family_id):
        raise ApiError(429, "daily_reading_limit", "가족당 하루 판독 한도 초과")


def normalize_taken_at(value: datetime | None) -> datetime:
    if value is None:
        return clock.now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=KST)
    return value


def store_photo(
    db: Session,
    settings: Settings,
    *,
    family_id: str,
    device_id: str,
    data: bytes,
    taken_at: datetime | None,
    dose_event_id: str | None = None,
    client_upload_id: str | None = None,
) -> Photo:
    media_type = check_upload(settings, data)
    photo = Photo(
        family_id=family_id,
        dose_event_id=dose_event_id,
        path="",
        media_type=media_type,
        taken_at=normalize_taken_at(taken_at),
        uploaded_by_device_id=device_id,
        client_upload_id=client_upload_id,
    )
    db.add(photo)
    db.flush()
    photo.path = save(settings, family_id, photo.id, data, media_type)
    return photo


def _failed(db: Session, reader: Reader, photo: Photo, kind: str, err: Exception, **kw) -> Reading:
    reading = Reading(
        family_id=photo.family_id,
        photo_id=photo.id,
        kind=kind,
        provider=reader.provider,
        model=reader.model,
        is_mock=reader.provider == "mock",
        error=str(err),
        **kw,
    )
    db.add(reading)
    return reading


def read_layout(
    db: Session, reader: Reader, photo: Photo, data: bytes
) -> tuple[Reading, LayoutProposal]:
    """실패하면 기록을 남기고 502 reading_failed (보호자 등록 화면용)."""
    try:
        proposal = reader.detect_layout(data)
    except ReaderError as e:
        _failed(db, reader, photo, "layout", e)
        db.commit()
        raise ApiError(502, "reading_failed", "AI 판독 실패") from e
    reading = Reading(
        family_id=photo.family_id,
        photo_id=photo.id,
        kind="layout",
        provider=proposal.provider,
        model=proposal.model,
        is_mock=proposal.is_mock,
        output=proposal.model_dump(exclude={"raw_response"}),
        raw_response=proposal.raw_response,
        latency_ms=proposal.latency_ms,
    )
    db.add(reading)
    return reading, proposal


def read_cells(
    db: Session,
    reader: Reader,
    photo: Photo,
    data: bytes,
    layout: Layout,
    dose_event_id: str | None = None,
) -> tuple[Reading, CellReadings | None]:
    """판독 실패면 (오류 기록, None). 호출한 쪽이 실패를 어떻게 다룰지 정한다."""
    try:
        result = reader.read_cells(data, layout)
    except ReaderError as e:
        return _failed(db, reader, photo, "cells", e, dose_event_id=dose_event_id), None
    reading = Reading(
        family_id=photo.family_id,
        photo_id=photo.id,
        dose_event_id=dose_event_id,
        kind="cells",
        provider=result.provider,
        model=result.model,
        is_mock=result.is_mock,
        output={"cells": [c.model_dump() for c in result.cells]},
        raw_response=result.raw_response,
        latency_ms=result.latency_ms,
    )
    db.add(reading)
    return reading, result


def baseline_cell_results(result: CellReadings, settings: Settings) -> list[dict]:
    """등록·리필 판독의 칸 결과. 기대 상태가 없으므로 expected=None."""
    out = []
    for c in result.cells:
        attention = None
        if c.state == "unclear":
            attention = "unclear"
        elif c.confidence < settings.normal_confidence:
            attention = "low_confidence"
        out.append(
            {
                "cell_id": c.cell_id,
                "ai_state": c.state,
                "expected": None,
                "attention": attention,
                "bbox": list(c.bbox) if c.bbox else None,
            }
        )
    return out
