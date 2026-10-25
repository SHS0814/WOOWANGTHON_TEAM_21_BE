"""처리할 일 목록, 인계 상세, 보호자 판정 (api.md 3.20~3.22, R-010 3번, R-014)."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_guardian, get_db, get_settings
from app.errors import ApiError, not_found
from app.models import Device, DoseEvent, Handoff, Reading
from app.serializers import base_url, handoff_out, person
from app.services import dose as ds
from app.services.expected import active_pillbox, latest_refill

router = APIRouter()

Verdict = Literal["taken", "not_taken", "acknowledged", "refilled", "not_refilled"]


@router.get("/handoffs")
def list_handoffs(
    request: Request,
    status: Literal["open", "closed", "all"] = "open",
    limit: int = Query(50, ge=1, le=200),
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    q = select(Handoff).where(Handoff.family_id == auth.family.id)
    if status == "open":
        q = q.where(Handoff.verdict.is_(None))
    elif status == "closed":
        q = q.where(Handoff.verdict.is_not(None))
    # 열린 건 먼저, 그다음 최신순
    q = q.order_by(Handoff.verdict.is_not(None), Handoff.notified_at.desc()).limit(limit)
    url = base_url(request, settings)
    return {"items": [handoff_out(db, settings, url, h, detail=False) for h in db.scalars(q)]}


def _get(db: Session, auth: Auth, handoff_id: str) -> Handoff:
    h = db.get(Handoff, handoff_id)
    if h is None or h.family_id != auth.family.id:
        raise not_found("인계")
    return h


@router.get("/handoffs/{handoff_id}")
def get_handoff(
    handoff_id: str,
    request: Request,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    h = _get(db, auth, handoff_id)
    return handoff_out(db, settings, base_url(request, settings), h, detail=True)


class VerdictIn(BaseModel):
    verdict: Verdict


@router.post("/handoffs/{handoff_id}/verdict")
def post_verdict(
    handoff_id: str,
    body: VerdictIn,
    request: Request,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _get(db, auth, handoff_id)
    # 두 보호자가 동시에 판정하는 경우를 막는다
    h = db.scalar(
        select(Handoff)
        .where(Handoff.id == handoff_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if h.verdict is not None:
        raise ApiError(
            409,
            "handoff_already_resolved",
            "다른 보호자가 먼저 판정함",
            {
                "verdict": h.verdict,
                "verdict_by": person(db.get(Device, h.verdict_by_device_id)),
            },
        )
    if body.verdict not in ds.ALLOWED_VERDICTS[h.reason]:
        raise ApiError(422, "verdict_not_allowed", "이 사유에 맞지 않는 판정")

    now = clock.now()
    h.verdict = body.verdict
    h.verdict_by_device_id = auth.device.id
    h.verdict_at = now
    event = db.get(DoseEvent, h.dose_event_id)

    if h.reason == "refill_unconfirmed":
        _refill_answer(db, request, auth, h, event, now)
    elif h.binds_event:
        _resolve(event, body.verdict, auth.device.id, now)
    db.commit()
    return handoff_out(db, settings, base_url(request, settings), h, detail=True)


def _resolve(event: DoseEvent, outcome: str, device_id: str, now) -> None:
    event.state = "done"
    event.outcome = outcome
    event.outcome_source = "guardian"
    event.decided_by_device_id = device_id
    event.resolved_at = now
    event.retake_deadline_at = None
    # 늦게 온 정상 사진이 있으면 그 촬영 시각을 기록 시각으로 둔다 (R-021 Q-004).
    # acknowledged 는 기록을 지우지 않는다. not_taken 은 보호자가 안 드셨다고 판정한 것
    if outcome == "taken":
        event.taken_at = event.taken_at or now
    elif outcome == "not_taken":
        event.taken_at = None


def _refill_answer(
    db: Session, request: Request, auth: Auth, h: Handoff, event: DoseEvent, now
) -> None:
    reading = db.get(Reading, h.reading_id)
    pillbox = active_pillbox(db, auth.family.id)
    if h.verdict == "refilled":
        # 그 사진을 새 기준선으로 (backend.md 5.4)
        if pillbox is not None and reading is not None:
            ds.new_baseline_from_reading(db, auth.family.id, reading, auth.device.id, pillbox.id)
        if h.binds_event:
            target = next(
                (c for c in reading.cells if c["cell_id"] == reading.target_cell_id), None
            )
            if target is not None and target["ai_state"] == "empty":
                # R-014: 이미 쓴 칸이었지만 보호자가 채웠다고 확인 → 보호자 판정으로 복용 기록
                _resolve(event, "taken", auth.device.id, now)
        return
    # not_refilled: 차 있던 지난 칸을 비교 제외 (R-010 3번)
    if pillbox is not None and reading is not None:
        refill = latest_refill(db, pillbox.id)
        if refill is not None:
            full_past = [
                c["cell_id"]
                for c in reading.cells
                if c["cell_id"] != reading.target_cell_id
                and c["ai_state"] == "full"
                and c["expected"] == "empty"
            ]
            refill.ignored_cells = sorted(set(refill.ignored_cells or []) | set(full_past))
    if h.binds_event:
        # R-014: 복약 건은 판정 대기. 같은 건에 refill_overdue 인계를 푸시 없이 만든다
        ds.create_handoff(
            db,
            request.app.state.push,
            auth.family,
            event,
            "refill_overdue",
            now,
            reading=reading,
            send_push=False,
        )
