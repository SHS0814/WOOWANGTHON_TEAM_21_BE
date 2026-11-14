"""DB 테이블 (backend.md 4장)."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.clock import clock
from app.db import Base, UTCDateTime


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return clock.now()


class Family(Base):
    __tablename__ = "families"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    elder_display_name: Mapped[str] = mapped_column(String(40))
    summary_push_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    summary_push_time: Mapped[str] = mapped_column(String(5), default="21:00")
    photo_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    unchanged_mode: Mapped[str] = mapped_column(String(32), default="remind_then_retake")
    # 마지막 칸을 쓴 정상 기록 뒤 어르신에게 "약통을 채워 주세요" (R-071). 기존 DB 에는
    # nullable 열로 붙고(db._add_missing_columns) NULL 은 false 로 본다
    elder_refill_prompt: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # elder | guardian
    display_name: Mapped[str] = mapped_column(String(40))
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    push_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paired_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consent_text_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 알림 준비 상태 (R-032). 보고 전이면 notification_status_at 이 None
    notifications_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exact_alarm_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notification_status_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PairingCode(Base):
    __tablename__ = "pairing_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # elder | guardian
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    short_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by_device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    dose_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("dose_events.id"), nullable=True, index=True
    )
    path: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(32))
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    uploaded_by_device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    client_upload_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 복약 사진의 처리 결과 (같은 client_upload_id 재전송에 같은 결과를 돌려주기 위해)
    upload_result: Mapped[str | None] = mapped_column(String(24), nullable=True)
    upload_is_mock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    upload_refill_prompt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Reading(Base):
    """AI 판독 결과 원본 (backend.md 4장 readings)."""

    __tablename__ = "readings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    photo_id: Mapped[str] = mapped_column(ForeignKey("photos.id"), index=True)
    dose_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("dose_events.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # layout | cells
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    is_mock: Mapped[bool] = mapped_column(Boolean)
    # 판독기 원본 결과 (cells: confidence 포함, layout: 제안). 보호자 API로 신뢰도 숫자는 내보내지 않음
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    # 기대 상태와 비교한 칸별 결과 (api.md CellResult). 등록·리필 판독은 expected=None
    cells: Mapped[list] = mapped_column(JSON, default=list)
    target_cell_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


class Pillbox(Base):
    __tablename__ = "pillboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    layout: Mapped[dict] = mapped_column(JSON)
    registered_photo_id: Mapped[str] = mapped_column(ForeignKey("photos.id"))
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    pillbox_id: Mapped[str] = mapped_column(ForeignKey("pillboxes.id"))
    weekday: Mapped[int] = mapped_column(Integer)  # 0=월
    time_of_day: Mapped[str] = mapped_column(String(16))
    dose_time: Mapped[str] = mapped_column(String(5))  # HH:MM
    cell_id: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Refill(Base):
    """기준선 (backend.md 5.1). baseline_cells: [{cell_id, state: full|empty, edited}]."""

    __tablename__ = "refills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    pillbox_id: Mapped[str] = mapped_column(ForeignKey("pillboxes.id"))
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    filled_by_device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    photo_id: Mapped[str] = mapped_column(ForeignKey("photos.id"))
    baseline_reading_id: Mapped[str] = mapped_column(ForeignKey("readings.id"))
    baseline_cells: Mapped[list] = mapped_column(JSON)
    # 리필 확인 "아니요"로 비교에서 뺀 칸 (R-010 3번)
    ignored_cells: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(
        String(16)
    )  # registration | guardian_photo | refill_check | auto_refill
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class DoseEvent(Base):
    __tablename__ = "dose_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    slot_id: Mapped[str] = mapped_column(ForeignKey("schedule_slots.id"), index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # 서울 기준 YYYY-MM-DD
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    state: Mapped[str] = mapped_column(String(24), default="scheduled")
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_by_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id"), nullable=True
    )
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    retake_count: Mapped[int] = mapped_column(Integer, default=0)
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    retake_deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    family_id: Mapped[str] = mapped_column(ForeignKey("families.id"), index=True)
    dose_event_id: Mapped[str] = mapped_column(ForeignKey("dose_events.id"), index=True)
    reason: Mapped[str] = mapped_column(String(24))
    reading_id: Mapped[str | None] = mapped_column(ForeignKey("readings.id"), nullable=True)
    notified_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    late_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    # 복약 건의 결과를 정하는 인계인지. 대상 칸과 무관한 리필 확인 요청(5.4)만 False (R-014)
    binds_event: Mapped[bool] = mapped_column(Boolean, default=True)
    # 보호자에게 푸시를 보냈는지. 재알림 금지(C2)로 푸시 없이 만든 인계는 False
    pushed: Mapped[bool] = mapped_column(Boolean, default=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verdict_by_device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id"), nullable=True
    )
    verdict_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# 같은 칸·같은 날의 복약 건은 하나 (동시 요청에서 중복 생성 방지)
Index("uq_dose_event_slot_date", DoseEvent.slot_id, DoseEvent.date, unique=True)

# 같은 복약 건에 같은 사유의 열린 인계는 하나만 (R-007 C2, R-018). 동시 요청 경합을 DB 에서 막는다
_OPEN_BOUND = text("verdict IS NULL AND binds_event")
_OPEN_SIDE_REFILL = text("verdict IS NULL AND NOT binds_event AND reason = 'refill_unconfirmed'")
Index(
    "uq_open_handoff_event_reason",
    Handoff.dose_event_id,
    Handoff.reason,
    unique=True,
    postgresql_where=_OPEN_BOUND,
    sqlite_where=_OPEN_BOUND,
)
# 대상 칸과 무관한 리필 확인 요청(5.4)은 가족당 하나만 열려 있다
Index(
    "uq_open_side_refill_check",
    Handoff.family_id,
    unique=True,
    postgresql_where=_OPEN_SIDE_REFILL,
    sqlite_where=_OPEN_SIDE_REFILL,
)
