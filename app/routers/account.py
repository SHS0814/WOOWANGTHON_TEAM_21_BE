"""내 정보, 기기 관리, 푸시 토큰, 설정 (api.md 3.6~3.9, 3.26)."""

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_guardian, auth_preconsent, get_db, get_settings
from app.errors import ApiError, not_found
from app.models import Device, Pillbox, ScheduleSlot
from app.schemas import DisplayName
from app.serializers import active_guardians, device_out, person

router = APIRouter()


@router.get("/me")
def me(
    auth: Auth = Depends(auth_preconsent),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    family = auth.family
    has_pillbox = (
        db.scalar(select(Pillbox.id).where(Pillbox.family_id == family.id, Pillbox.active))
        is not None
    )
    has_schedule = (
        db.scalar(
            select(ScheduleSlot.id).where(ScheduleSlot.family_id == family.id, ScheduleSlot.active)
        )
        is not None
    )
    return {
        "device": device_out(auth.device, auth.device.id),
        "consent_required": auth.device.role == "elder" and auth.device.consent_at is None,
        "family": {
            "id": family.id,
            "elder_display_name": family.elder_display_name,
            "guardians": [person(g) for g in active_guardians(db, family.id)],
            "has_pillbox": has_pillbox,
            "has_schedule": has_schedule,
            "is_demo": family.is_demo,
        },
        "server": {
            "timezone": settings.timezone,
            "timing": {
                "reask_offsets_min": settings.reask_offsets_min,
                "no_response_handoff_min": settings.no_response_handoff_min,
                "retake_wait_min": settings.retake_wait_min,
            },
            "reader_is_mock": settings.reader_is_mock,
            "ai_external_transfer": settings.ai_external_transfer,
            "consent_text_version": settings.consent_text_version,
            "demo_mode": settings.demo_mode,
        },
    }


@router.get("/devices")
def list_devices(auth: Auth = Depends(auth_guardian), db: Session = Depends(get_db)):
    devices = db.scalars(
        select(Device)
        .where(Device.family_id == auth.family.id, Device.revoked_at.is_(None))
        .order_by(Device.paired_at)
    )
    return {"items": [device_out(d, auth.device.id) for d in devices]}


@router.delete("/devices/{device_id}", status_code=204)
def revoke_device(
    device_id: str, auth: Auth = Depends(auth_guardian), db: Session = Depends(get_db)
):
    device = db.get(Device, device_id)
    if device is None or device.family_id != auth.family.id or device.revoked_at is not None:
        raise not_found("기기")
    if device.role == "guardian" and len(active_guardians(db, auth.family.id)) <= 1:
        raise ApiError(409, "last_guardian", "마지막 보호자는 해제할 수 없음")
    device.revoked_at = clock.now()
    db.commit()
    return Response(status_code=204)


class PushTokenIn(BaseModel):
    push_token: str | None = Field(max_length=255)


@router.put("/devices/me/push-token", status_code=204)
def set_push_token(
    body: PushTokenIn, auth: Auth = Depends(auth_preconsent), db: Session = Depends(get_db)
):
    auth.device.push_token = body.push_token
    db.commit()
    return Response(status_code=204)


class SettingsIn(BaseModel):
    elder_display_name: DisplayName | None = None
    summary_push_enabled: bool | None = None
    summary_push_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    photo_retention_days: int | None = Field(default=None, ge=1, le=365)
    unchanged_mode: Literal["remind_then_retake", "handoff_immediately"] | None = None


def _settings_out(auth: Auth) -> dict:
    f = auth.family
    return {
        "elder_display_name": f.elder_display_name,
        "summary_push_enabled": f.summary_push_enabled,
        "summary_push_time": f.summary_push_time,
        "photo_retention_days": f.photo_retention_days,
        "unchanged_mode": f.unchanged_mode,
    }


@router.get("/settings")
def get_family_settings(auth: Auth = Depends(auth_guardian)):
    return _settings_out(auth)


@router.put("/settings")
def put_family_settings(
    body: SettingsIn, auth: Auth = Depends(auth_guardian), db: Session = Depends(get_db)
):
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(auth.family, key, value)
    if body.elder_display_name is not None:
        # 어르신 기기의 표시 이름은 가족 호칭을 따른다
        for d in db.scalars(
            select(Device).where(Device.family_id == auth.family.id, Device.role == "elder")
        ):
            d.display_name = auth.family.elder_display_name
    db.commit()
    return _settings_out(auth)
