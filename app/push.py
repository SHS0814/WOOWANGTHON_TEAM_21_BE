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
