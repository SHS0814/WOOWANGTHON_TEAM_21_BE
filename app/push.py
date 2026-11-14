"""보호자 푸시. FCM HTTP v1 직접 발송(R-039), Expo 발송, 기록용이 있고 PUSH_PROVIDER 로 고른다."""

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import after_commit
from app.models import Device

log = logging.getLogger(__name__)


@dataclass
class PushMessage:
    device_id: str
    push_token: str | None
    title: str
    body: str
    data: dict = field(default_factory=dict)
    # True 면 화면에 보이지 않는 데이터 전용 푸시 (어르신 폰이 알람을 다시 맞추게 깨우는 용도)
    silent: bool = False


class PushSender(Protocol):
    def send(self, messages: list[PushMessage]) -> None: ...


class RecordingPushSender:
    """보내지 않고 기록만 한다 (개발·테스트용)."""

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    def send(self, messages: list[PushMessage]) -> None:
        for m in messages:
            log.info("push(skip) to %s: %s / %s %s", m.device_id, m.title, m.body, m.data)
        self.sent.extend(messages)


def notify_guardians(
    db: Session,
    sender: PushSender,
    family_id: str,
    title: str,
    body: str,
    data: dict,
    exclude_device_id: str | None = None,
) -> None:
    """보호자 전원에게 푸시. **커밋이 성공한 뒤에** 보낸다 (롤백되면 보내지 않음).

    호출한 쪽은 이후에 반드시 db.commit() 한다.
    """
    guardians = db.scalars(
        select(Device).where(
            Device.family_id == family_id,
            Device.role == "guardian",
            Device.revoked_at.is_(None),
        )
    ).all()
    messages = [
        PushMessage(g.id, g.push_token, title, body, data)
        for g in guardians
        if g.id != exclude_device_id
    ]
    after_commit(db, lambda: sender.send(messages))


def notify_elder_schedule_changed(db: Session, sender: PushSender, family_id: str) -> None:
    """복약 시각이 바뀌었다고 어르신 기기에 데이터 전용 푸시를 보낸다. **커밋이 성공한 뒤에** 보낸다.

    어르신 폰의 복약 알람은 폰이 스스로 예약하는 로컬 알람이라, 앱이 꺼져 있으면 바뀐 시각을 모른다.
    이 푸시를 받은 앱이 백그라운드에서 일정을 다시 읽어 알람을 다시 맞춘다. 화면에는 아무것도 보이지 않는다.
    푸시가 닿지 않아도 앱을 열면 다시 맞춰지므로 실패는 로그만 남긴다. 호출한 쪽은 이후에 db.commit() 한다.
    """
    elders = db.scalars(
        select(Device).where(
            Device.family_id == family_id,
            Device.role == "elder",
            Device.revoked_at.is_(None),
        )
    ).all()
    messages = [
        PushMessage(e.id, e.push_token, "", "", {"type": "schedule_changed"}, silent=True)
        for e in elders
        if e.push_token  # 토큰을 등록하지 않은 기기(옛 앱)는 보낼 곳이 없다
    ]
    if messages:
        after_commit(db, lambda: sender.send(messages))


class ExpoPushSender:
    """Expo Push API 로 보낸다 (https://docs.expo.dev/push-notifications/sending-notifications/).

    푸시 토큰이 없는 기기는 건너뛴다. 실패는 로그만 남기고 요청 흐름을 막지 않는다.
    """

    URL = "https://exp.host/--/api/v2/push/send"

    def __init__(
        self, access_token: str | None = None, timeout_s: float = 5.0, transport=None
    ) -> None:
        import httpx

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.Client(headers=headers, timeout=timeout_s, transport=transport)
        self.sent: list[PushMessage] = []

    def send(self, messages: list[PushMessage]) -> None:
        # 데이터 전용 푸시는 FCM 직접 발송에서만 지원한다
        targets = [m for m in messages if m.push_token and not m.silent]
        for i in range(0, len(targets), 100):  # Expo 는 한 번에 최대 100건
            batch = targets[i : i + 100]
            body = [
                {
                    "to": m.push_token,
                    "title": m.title,
                    "body": m.body,
                    "data": m.data,
                    "sound": "default",
                    "priority": "high",
                }
                for m in batch
            ]
            try:
                res = self._client.post(self.URL, json=body)
                res.raise_for_status()
                for m, ticket in zip(batch, res.json().get("data", []), strict=False):
                    if ticket.get("status") != "ok":
                        log.warning("expo push error for %s: %s", m.device_id, ticket)
                self.sent.extend(batch)
            except Exception:
                log.exception("expo push failed (%d messages)", len(batch))


class FcmConfigError(Exception):
    """서비스 계정 파일이 없거나 형식이 맞지 않음."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FcmPushSender:
    """FCM HTTP v1 로 직접 보낸다 (R-039, https://firebase.google.com/docs/cloud-messaging/send/v1-api).

    액세스 토큰은 서비스 계정 키로 서명한 JWT 로 받고, 만료 1분 전까지 다시 쓴다.
    푸시 토큰이 없는 기기는 건너뛴다. 실패는 로그만 남기고 요청 흐름을 막지 않는다.
    FCM 이 토큰이 무효라고 하면 forget_token(device_id, token) 을 불러 그 기기의 토큰을 지운다.
    """

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
    # 기기 토큰이 더는 쓸 수 없다는 FCM 오류 코드
    INVALID_TOKEN_CODES = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH"})

    def __init__(
        self,
        service_account_file: str,
        *,
        channel_id: str = "guardian",
        forget_token: Callable[[str, str], None] | None = None,
        validate_only: bool = False,
        timeout_s: float = 5.0,
        transport=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        import httpx
        from cryptography.hazmat.primitives import serialization

        path = Path(service_account_file).expanduser()
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            self._project_id = info["project_id"]
            self._client_email = info["client_email"]
            self._token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
            self._key = serialization.load_pem_private_key(
                info["private_key"].encode(), password=None
            )
        except FileNotFoundError as e:
            raise FcmConfigError(f"file not found: {path}") from e
        except OSError as e:
            raise FcmConfigError(f"cannot read {path}: {type(e).__name__}") from e
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            # 키 내용이 로그에 나오지 않도록 예외 종류만 남긴다
            raise FcmConfigError(f"invalid service account file {path}: {type(e).__name__}") from e
        self.url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        self._channel_id = channel_id
        self._forget_token = forget_token
        self._validate_only = validate_only
        self._clock = clock
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._access_token: str | None = None
        self._access_expires_at = 0.0
        self.sent: list[PushMessage] = []

    def _assertion(self, now: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self._client_email,
            "scope": self.SCOPE,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = ".".join(
            _b64url(json.dumps(p, separators=(",", ":")).encode()) for p in (header, claims)
        )
        sig = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(sig)}"

    def access_token(self) -> str:
        now = self._clock()
        if self._access_token and now < self._access_expires_at - 60:
            return self._access_token
        res = self._client.post(
            self._token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(int(now)),
            },
        )
        res.raise_for_status()
        body = res.json()
        self._access_token = body["access_token"]
        self._access_expires_at = now + float(body.get("expires_in", 3600))
        return self._access_token

    def _payload(self, m: PushMessage) -> dict:
        data = {
            k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            for k, v in m.data.items()
        }
        if m.silent:
            # notification 을 넣지 않아야 앱이 꺼져 있어도 앱의 백그라운드 처리로 전달된다
            return {
                "validate_only": self._validate_only,
                "message": {
                    "token": m.push_token,
                    "data": data,
                    "android": {"priority": "HIGH"},
                },
            }
        return {
            "validate_only": self._validate_only,
            "message": {
                "token": m.push_token,
                "notification": {"title": m.title, "body": m.body},
                # FCM data 는 문자열 값만 받는다
                "data": {
                    k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in m.data.items()
                },
                # 높은 우선순위 + 앱의 보호자 채널, 잠금 화면에서도 내용을 보인다
                "android": {
                    "priority": "HIGH",
                    "notification": {
                        "channel_id": self._channel_id,
                        "sound": "default",
                        "notification_priority": "PRIORITY_HIGH",
                        "visibility": "PUBLIC",
                    },
                },
            },
        }

    def _post(self, m: PushMessage):
        res = self._client.post(
            self.url,
            json=self._payload(m),
            headers={"Authorization": f"Bearer {self.access_token()}"},
        )
        if res.status_code == 401:  # 토큰이 서버 쪽에서 먼저 만료됨. 새로 받아 한 번만 다시
            self._access_token = None
            res = self._client.post(
                self.url,
                json=self._payload(m),
                headers={"Authorization": f"Bearer {self.access_token()}"},
            )
        return res

    def send(self, messages: list[PushMessage]) -> None:
        for m in messages:
            if not m.push_token:
                continue
            try:
                res = self._post(m)
                if res.status_code == 200:
                    self.sent.append(m)
                    continue
                code = _fcm_error_code(res)
                log.warning("fcm push error for %s: %s %s", m.device_id, res.status_code, code)
                if code in self.INVALID_TOKEN_CODES or (
                    code == "INVALID_ARGUMENT" and _bad_token_field(res)
                ):
                    self._forget(m)
            except Exception:
                log.exception("fcm push failed for %s", m.device_id)

    def _forget(self, m: PushMessage) -> None:
        if self._forget_token is None:
            return
        try:
            self._forget_token(m.device_id, m.push_token)
            log.info("fcm token of %s is invalid; cleared", m.device_id)
        except Exception:
            log.exception("clearing push token of %s failed", m.device_id)


def _fcm_error(res) -> dict:
    try:
        return res.json().get("error") or {}
    except ValueError:
        return {}


def _fcm_error_code(res) -> str | None:
    """FCM 상세 오류 코드 (FcmError.errorCode). 없으면 google.rpc 상태 문자열."""
    err = _fcm_error(res)
    for d in err.get("details") or []:
        if d.get("@type", "").endswith("google.firebase.fcm.v1.FcmError") and d.get("errorCode"):
            return d["errorCode"]
    return err.get("status")


def _bad_token_field(res) -> bool:
    """INVALID_ARGUMENT 가 메시지 내용이 아니라 기기 토큰 형식 때문인지."""
    for d in _fcm_error(res).get("details") or []:
        for v in d.get("fieldViolations") or []:
            if v.get("field") == "message.token":
                return True
    return False


def clear_push_token(sessionmaker) -> Callable[[str, str], None]:
    """무효 토큰 삭제. 그사이 앱이 새 토큰을 등록했으면 건드리지 않는다."""

    def forget(device_id: str, token: str) -> None:
        with sessionmaker() as db:
            db.execute(
                update(Device)
                .where(Device.id == device_id, Device.push_token == token)
                .values(push_token=None)
            )
            db.commit()

    return forget


def build_push_sender(settings, sessionmaker) -> PushSender:
    """PUSH_PROVIDER 로 고른다. FCM 설정이 잘못되면 오류를 남기고 기록만 하는 쪽으로 뜬다."""
    provider = settings.effective_push_provider
    if provider == "fcm":
        if not settings.fcm_service_account_file:
            log.error(
                "PUSH_PROVIDER=fcm 인데 FCM_SERVICE_ACCOUNT_FILE 이 비어 있음. 푸시를 보내지 않는다"
            )
            return RecordingPushSender()
        try:
            return FcmPushSender(
                settings.fcm_service_account_file,
                channel_id=settings.fcm_android_channel_id,
                forget_token=clear_push_token(sessionmaker),
            )
        except FcmConfigError as e:
            log.error("FCM 을 쓸 수 없음: %s. 푸시를 보내지 않는다", e)
            return RecordingPushSender()
    if provider == "expo":
        return ExpoPushSender(settings.expo_access_token)
    if provider != "recording":
        log.error("알 수 없는 PUSH_PROVIDER %r. 푸시를 보내지 않는다", provider)
    return RecordingPushSender()
