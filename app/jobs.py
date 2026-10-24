"""예약 작업 (backend.md 9장). 서버 프로세스 안에서 1분마다 run_once 를 부른다.

- 복약 건 만들기·열기·기한 넘긴 건 인계: services.dose.tick (API 요청 때도 같은 함수를 부른다)
- 하루·주간 요약 푸시: 보호자가 켰을 때만, 가족 설정 시각에 1건 (기본은 앱 안에서만 보여줌)
- 정리: 만료된 짝짓기 코드 삭제, 보관 기간이 지난 사진 삭제 (매시 정각)
"""

import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.models import Family, PairingCode, Photo
from app.push import PushSender, notify_guardians
from app.services import dose as ds
from app.services.summary import current_refill, summarize, week_start
from app.storage import full_path
from app.timeutil import KST, kst_date

log = logging.getLogger(__name__)


def summary_body(s: dict, family: Family) -> str:
    c = s["counts"]
    unit = "오늘" if s["period"] == "day" else "이번 주"
    if s["all_normal"]:
        text = f"{unit} {c['scheduled']}번 모두 정상이에요"
    else:
        taken = c["taken_ai"] + c["taken_guardian"]
        text = f"{unit} {c['scheduled']}번 중 {taken}번 기록, 확인할 일 {c['open_handoffs']}건"
    if s["refill"] and s["refill"]["needs_refill"]:
        text += f" · 약통이 {s['refill']['remaining_cells']}칸 남았어요"
    if s["pillbox_mismatch"]:
        text += " · 약통 상태가 기록과 달라요"
    if s["includes_mock"]:
        text += " (모의 판독 포함)"
    return f"{family.elder_display_name} {text}"


def _summary_push(
    db: Session, settings: Settings, push: PushSender, family: Family, now: datetime, sent: set
) -> None:
    local = now.astimezone(KST)
    if not family.summary_push_enabled or local.strftime("%H:%M") != family.summary_push_time:
        return
    today = kst_date(now)
    refill = current_refill(db, settings, family, now)
    periods = [("day", today)]
    if today.weekday() == 6:  # 일요일
        periods.append(("week", week_start(today)))
    for period, start in periods:
        key = (family.id, period, start.isoformat())
        if key in sent:
            continue
        s = summarize(db, settings, family, period, start, now, refill)
        notify_guardians(
            db,
            push,
            family.id,
            title="하루 요약" if period == "day" else "주간 요약",
            body=summary_body(s, family),
            data={"type": "summary", "period": period, "period_start": start.isoformat()},
        )
        sent.add(key)


def cleanup(db: Session, settings: Settings, now: datetime) -> None:
    db.execute(delete(PairingCode).where(PairingCode.expires_at < now - timedelta(days=1)))
    for family in db.scalars(select(Family)):
        cutoff = now - timedelta(days=family.photo_retention_days)
        for photo in db.scalars(
            select(Photo).where(
                Photo.family_id == family.id,
                Photo.deleted_at.is_(None),
                Photo.uploaded_at < cutoff,
            )
        ):
            full_path(settings, photo).unlink(missing_ok=True)
            photo.deleted_at = now


def run_once(app: FastAPI, now: datetime | None = None) -> None:
    now = now or clock.now()
    settings: Settings = app.state.settings
    push: PushSender = app.state.push
    with app.state.sessionmaker() as db:
        for family in db.scalars(select(Family)).all():
            try:
                ds.tick(db, settings, push, family, now)
                _summary_push(db, settings, push, family, now, app.state.summary_sent)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("job failed for family %s", family.id)
        if now.astimezone(KST).minute == 0:
            cleanup(db, settings, now)
            db.commit()


async def loop(app: FastAPI, interval_s: float = 60.0) -> None:
    while True:
        try:
            await asyncio.to_thread(run_once, app)
        except Exception:
            log.exception("jobs loop failed")
        await asyncio.sleep(interval_s)
