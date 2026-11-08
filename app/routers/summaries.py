"""하루·주간 요약 (api.md 3.25)."""

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.clock import clock
from app.config import Settings
from app.deps import Auth, auth_guardian, get_db, get_settings
from app.errors import validation
from app.services.summary import summaries, total_summary, week_start
from app.timeutil import kst_date

router = APIRouter()


@router.get("/summaries")
def get_summaries(
    request: Request,
    period: Literal["day", "week", "total"],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    auth: Auth = Depends(auth_guardian),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    now = clock.now()
    if period == "total":
        # from·to 는 무시한다 (R-051)
        return {"items": [total_summary(db, settings, auth.family, now)]}
    today = kst_date(now)
    to = to or today
    if from_ is None:
        from_ = to - timedelta(days=6) if period == "day" else week_start(to) - timedelta(weeks=3)
    if from_ > to:
        raise validation("from", "from 이 to 보다 늦음")
    # 하루 요약 최대 366일 (R-051), 주간 요약 최대 12주
    limit = 366 if period == "day" else 7 * 12
    if (to - from_).days >= limit:
        raise validation("to", "기간이 너무 김 (하루 요약 366일, 주간 요약 12주)")
    return {"items": summaries(db, settings, auth.family, period, from_, to, now)}
