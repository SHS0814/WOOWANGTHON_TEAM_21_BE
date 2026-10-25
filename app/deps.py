"""요청 의존성: DB 세션, 설정, 기기 토큰 인증 (api.md 1.2)."""

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.errors import ApiError
from app.models import Device, Family
from app.security import hash_secret


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


@dataclass
class Auth:
    device: Device
    family: Family

    @property
    def is_guardian(self) -> bool:
        return self.device.role == "guardian"


def _authenticate(request: Request, db: Session) -> Auth:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "unauthorized", "토큰 없음")
    device = db.scalar(select(Device).where(Device.token_hash == hash_secret(token.strip())))
    if device is None:
        raise ApiError(401, "unauthorized", "없는 토큰")
    if device.revoked_at is not None:
        raise ApiError(401, "token_revoked", "해제된 기기")
    device.last_seen_at = clock.now()
    db.commit()
    family = db.get(Family, device.family_id)
    return Auth(device=device, family=family)


def auth_preconsent(request: Request, db: Session = Depends(get_db)) -> Auth:
    """어르신 동의 전에도 쓸 수 있는 API (GET /me, POST /consent, push-token, notification-status)."""
    return _authenticate(request, db)


def auth_any(request: Request, db: Session = Depends(get_db)) -> Auth:
    auth = _authenticate(request, db)
    if auth.device.role == "elder" and auth.device.consent_at is None:
        raise ApiError(403, "consent_required", "어르신 동의가 필요합니다.")
    return auth


def auth_guardian(auth: Auth = Depends(auth_any)) -> Auth:
    if auth.device.role != "guardian":
        raise ApiError(403, "forbidden_role", "보호자만 쓸 수 있음")
    return auth


def auth_elder(auth: Auth = Depends(auth_any)) -> Auth:
    if auth.device.role != "elder":
        raise ApiError(403, "forbidden_role", "어르신 기기만 쓸 수 있음")
    return auth


def client_ip(request: Request) -> str:
    # Cloudflare Tunnel 뒤에서는 원래 IP가 이 헤더에 온다
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )
