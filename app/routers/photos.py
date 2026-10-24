"""사진 서명 URL과 사진 바이트 (api.md 3.23, 3.24)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_any, get_db, get_settings
from app.errors import ApiError, not_found
from app.models import Photo
from app.security import verify_photo
from app.serializers import base_url
from app.storage import full_path, photo_out

router = APIRouter()


def _expired() -> ApiError:
    return ApiError(410, "photo_expired", "보관 기간이 지나 삭제된 사진")


@router.get("/photos/{photo_id}")
def get_photo(
    photo_id: str,
    request: Request,
    auth: Auth = Depends(auth_any),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    photo = db.get(Photo, photo_id)
    if photo is None or photo.family_id != auth.family.id:
        raise not_found("사진")
    if photo.deleted_at is not None:
        raise _expired()
    return photo_out(settings, base_url(request, settings), photo)


@router.get("/photo-files/{photo_id}")
def get_photo_file(
    photo_id: str,
    exp: int,
    sig: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not verify_photo(settings.photo_url_secret, photo_id, exp, sig):
        raise ApiError(403, "signature_invalid", "서명 오류")
    if exp < int(clock.now().timestamp()):
        raise ApiError(403, "signature_expired", "서명 만료")
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise not_found("사진")
    path = full_path(settings, photo)
    if photo.deleted_at is not None or not path.exists():
        raise _expired()
    return FileResponse(
        path, media_type=photo.media_type, headers={"Cache-Control": "private, max-age=300"}
    )
