"""토큰·코드 생성과 해시, 사진 URL 서명."""

import hashlib
import hmac
import secrets


def new_token() -> str:
    """기기 토큰. 256비트, base64url 43자."""
    return secrets.token_urlsafe(32)


def new_pair_code() -> str:
    """짝짓기·초대 코드. 128비트, base64url 22자."""
    return secrets.token_urlsafe(16)


def new_short_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sign_photo(secret: str, photo_id: str, exp: int) -> str:
    msg = f"{photo_id}.{exp}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_photo(secret: str, photo_id: str, exp: int, sig: str) -> bool:
    return hmac.compare_digest(sign_photo(secret, photo_id, exp), sig)
