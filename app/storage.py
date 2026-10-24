"""사진 파일 저장과 서명 URL (backend.md 11장)."""

from datetime import datetime, timedelta
from pathlib import Path

from app.clock import clock
from app.config import Settings
from app.errors import ApiError
from app.models import Photo
from app.security import sign_photo
from app.timeutil import iso

EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}


def sniff_image(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    raise ApiError(415, "unsupported_media", "이미지가 아님 (JPEG/PNG/WEBP/HEIC)")


def check_upload(settings: Settings, data: bytes) -> str:
    if len(data) > settings.photo_max_bytes:
        raise ApiError(413, "photo_too_large", "사진이 너무 큼")
    if not data:
        raise ApiError(415, "unsupported_media", "빈 파일")
    return sniff_image(data)


def save(settings: Settings, family_id: str, photo_id: str, data: bytes, media_type: str) -> str:
    rel = Path(family_id) / f"{photo_id}.{EXT[media_type]}"
    dest = Path(settings.photo_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(rel)


def full_path(settings: Settings, photo: Photo) -> Path:
    return Path(settings.photo_dir) / photo.path


def load(settings: Settings, photo: Photo) -> bytes:
    return full_path(settings, photo).read_bytes()


def photo_out(settings: Settings, base_url: str, photo: Photo, now: datetime | None = None) -> dict:
    now = now or clock.now()
    expires = now + timedelta(seconds=settings.photo_url_ttl_s)
    exp = int(expires.timestamp())
    sig = sign_photo(settings.photo_url_secret, photo.id, exp)
    return {
        "id": photo.id,
        "taken_at": iso(photo.taken_at),
        "url": f"{base_url}/api/v1/photo-files/{photo.id}?exp={exp}&sig={sig}",
        "url_expires_at": iso(expires),
    }
