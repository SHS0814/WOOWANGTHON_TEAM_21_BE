"""가족 만들기, 짝짓기 코드, 기기 연결, 어르신 동의 (api.md 3.2~3.5, backend.md 3장)."""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_guardian, auth_preconsent, client_ip, get_db, get_settings
from app.errors import ApiError, validation
from app.models import Device, Family, PairingCode
from app.push import notify_guardians
from app.security import hash_secret, new_pair_code, new_short_code, new_token
from app.serializers import active_guardians, device_out, person
from app.timeutil import iso

router = APIRouter()

Name = Field(min_length=1, max_length=20)
Platform = Literal["android", "ios"] | None


class FamilyIn(BaseModel):
    elder_display_name: str = Name
    guardian_display_name: str = Name
    platform: Platform = None
    is_demo: bool = False


@router.post("/families", status_code=201)
def create_family(
    body: FamilyIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    limiter = request.app.state.limits["families"]
    now = clock.now()
    limiter.check(client_ip(request), now)
    limiter.hit(client_ip(request), now)
    if body.is_demo and not settings.demo_mode:
        raise validation("is_demo", "DEMO_MODE 가 꺼진 서버에서는 만들 수 없음")
    family = Family(elder_display_name=body.elder_display_name.strip(), is_demo=body.is_demo)
    db.add(family)
    db.flush()
    token = new_token()
    device = Device(
        family_id=family.id,
        role="guardian",
        display_name=body.guardian_display_name.strip(),
        platform=body.platform,
        token_hash=hash_secret(token),
    )
    db.add(device)
    db.commit()
    return {"family_id": family.id, "token": token, "device": device_out(device, device.id)}


class PairingCodeIn(BaseModel):
    kind: Literal["elder", "guardian"]


def _unique_short_code(db: Session) -> str:
    now = clock.now()
    for _ in range(20):
        code = new_short_code()
        clash = db.scalar(
            select(PairingCode.id).where(
                PairingCode.short_code_hash == hash_secret(code),
                PairingCode.used_at.is_(None),
                PairingCode.revoked_at.is_(None),
                PairingCode.expires_at > now,
            )
        )
        if clash is None:
            return code
    raise ApiError(500, "internal_error", "예비 코드 생성 실패")


@router.post("/pairing-codes", status_code=201)
def create_pairing_code(
    body: PairingCodeIn,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    now = clock.now()
    code = new_pair_code()
    short_code = None
    if body.kind == "elder":
        # 가족당 유효한 어르신 연결 코드는 1개 (R-010 7번)
        for old in db.scalars(
            select(PairingCode).where(
                PairingCode.family_id == auth.family.id,
                PairingCode.kind == "elder",
                PairingCode.used_at.is_(None),
                PairingCode.revoked_at.is_(None),
            )
        ):
            old.revoked_at = now
        short_code = _unique_short_code(db)
        expires = now + timedelta(minutes=settings.elder_code_ttl_min)
    else:
        expires = now + timedelta(hours=settings.guardian_code_ttl_h)
    db.add(
        PairingCode(
            family_id=auth.family.id,
            kind=body.kind,
            code_hash=hash_secret(code),
            short_code_hash=hash_secret(short_code) if short_code else None,
            created_by_device_id=auth.device.id,
            expires_at=expires,
        )
    )
    db.commit()
    return {
        "kind": body.kind,
        "code": code,
        "short_code": short_code,
        "expires_at": iso(expires),
        "qr_payload": f"woowang://pair?code={code}",
    }


class PairIn(BaseModel):
    code: str | None = Field(default=None, max_length=64)
    short_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=20)
    platform: Platform = None

    @model_validator(mode="after")
    def _one_code(self) -> "PairIn":
        if (self.code is None) == (self.short_code is None):
            raise ValueError("code 와 short_code 중 하나만")
        return self


@router.post("/pair", status_code=201)
def pair(
    body: PairIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    now = clock.now()
    ip = client_ip(request)
    failures = request.app.state.limits["pair_fail"]
    failures.check(ip, now)

    if body.code is not None:
        pc = db.scalar(select(PairingCode).where(PairingCode.code_hash == hash_secret(body.code)))
    else:
        # 예비 6자리 코드는 어르신 연결에만 있다
        pc = db.scalar(
            select(PairingCode)
            .where(
                PairingCode.short_code_hash == hash_secret(body.short_code),
                PairingCode.kind == "elder",
            )
            .order_by(PairingCode.created_at.desc())
        )
    if pc is None:
        failures.hit(ip, now)
        raise ApiError(400, "code_invalid", "없는 코드")
    if pc.used_at is not None or pc.revoked_at is not None:
        failures.hit(ip, now)
        raise ApiError(410, "code_used", "이미 쓴 코드")
    if pc.expires_at <= now:
        failures.hit(ip, now)
        raise ApiError(410, "code_expired", "만료된 코드")

    family = db.get(Family, pc.family_id)
    token = new_token()
    if pc.kind == "elder":
        for old in db.scalars(
            select(Device).where(
                Device.family_id == family.id,
                Device.role == "elder",
                Device.revoked_at.is_(None),
            )
        ):
            old.revoked_at = now
        device = Device(
            family_id=family.id,
            role="elder",
            display_name=family.elder_display_name,
            platform=body.platform,
            token_hash=hash_secret(token),
        )
    else:
        if body.display_name is None:
            raise validation("display_name", "보호자 초대에는 이름이 필요함")
        device = Device(
            family_id=family.id,
            role="guardian",
            display_name=body.display_name.strip(),
            platform=body.platform,
            token_hash=hash_secret(token),
        )
    pc.used_at = now
    db.add(device)
    db.flush()
    if device.role == "guardian":
        notify_guardians(
            db,
            request.app.state.push,
            family.id,
            title="보호자 추가",
            body=f"{device.display_name}님이 보호자로 추가됐어요",
            data={"type": "guardian_joined", "device_id": device.id},
            exclude_device_id=device.id,
        )
    db.commit()

    return {
        "token": token,
        "device": device_out(device, device.id),
        "family": {
            "id": family.id,
            "elder_display_name": family.elder_display_name,
            "guardians": [person(g) for g in active_guardians(db, family.id)],
        },
        "consent_required": device.role == "elder",
    }


class ConsentIn(BaseModel):
    agreed: Literal[True]
    consent_text_version: str = Field(min_length=1, max_length=16)
    ai_external_transfer_shown: bool


@router.post("/consent")
def consent(
    body: ConsentIn,
    auth: Auth = Depends(auth_preconsent),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if auth.device.role != "elder":
        raise ApiError(403, "forbidden_role", "어르신 기기만 동의함")
    if settings.ai_external_transfer and not body.ai_external_transfer_shown:
        raise ApiError(422, "consent_incomplete", "외부 AI 전송 안내 없이 동의할 수 없음")
    if auth.device.consent_at is None:
        auth.device.consent_at = clock.now()
        auth.device.consent_text_version = body.consent_text_version
        db.commit()
    return {"consent_at": iso(auth.device.consent_at)}
