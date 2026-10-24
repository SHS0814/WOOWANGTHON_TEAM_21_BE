"""보호자 푸시. 지금은 인터페이스와 기록용 구현만 있다. Expo Push 발송은 마지막 단계에 붙인다."""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device

log = logging.getLogger(__name__)


@dataclass
class PushMessage:
    device_id: str
    push_token: str | None
    title: str
    body: str
    data: dict = field(default_factory=dict)


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
    guardians = db.scalars(
        select(Device).where(
            Device.family_id == family_id,
            Device.role == "guardian",
            Device.revoked_at.is_(None),
        )
    ).all()
    sender.send(
        [
            PushMessage(g.id, g.push_token, title, body, data)
            for g in guardians
            if g.id != exclude_device_id
        ]
    )


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
        targets = [m for m in messages if m.push_token]
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
