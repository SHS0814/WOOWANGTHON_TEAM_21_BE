"""기록 검색 (api.md 3.19 filter·reason·order)과 요약 366일·전체 (3.25), R-051.

시각: T0 = 2026-10-23(금) 07:00 KST. 과거 기록은 tests/history.py 규칙으로 DB 에 직접 넣는다.
"""

from collections import Counter
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import DoseEvent, Handoff, Photo, Reading
from app.routers.dose import HandoffReason
from app.serializers import event_out, events_out
from app.services.dose import ALLOWED_VERDICTS
from app.services.expected import mismatch_cells
from app.services.summary import PENDING
from tests.conftest import Family, week_schedule
from tests.history import seed_history
from tests.test_dose import fam  # noqa: F401 (fixture)

START = date(2025, 10, 23)  # 2026-10-22 까지 365일
END = date(2026, 10, 22)
API = "/api/v1"


@pytest.fixture
def hist(client, fam):  # noqa: F811
    with client.app.state.sessionmaker() as db:
        events = seed_history(db, fam.family_id, START, END)
        info = []
        for e in events:
            reasons = {
                h.reason for h in db.scalars(select(Handoff).where(Handoff.dose_event_id == e.id))
            }
            info.append((e.id, e.outcome, reasons))
    return fam, info


def get(client, f, q: str):
    return client.get(f"{API}/dose-events?{q}", headers=f.g)


def ids(res) -> list[str]:
    assert res.status_code == 200, res.text
    return [e["id"] for e in res.json()["items"]]


def err_field(res) -> str:
    assert res.status_code == 422, res.text
    body = res.json()["error"]
    assert body["code"] == "validation_error"
    return body["details"]["fields"][0]["field"]


YEAR = f"from={START}&to={END}"


def test_handoff_reason_matches_server_reasons():
    assert list(HandoffReason.__args__) == list(ALLOWED_VERDICTS)


@pytest.mark.parametrize(
    ("q", "field"),
    [
        ("filter=bogus", "filter"),
        ("reason=bogus", "reason"),
        ("order=newest", "order"),
        ("filter=", "filter"),
    ],
)
def test_bad_query_values(client, fam, q, field):  # noqa: F811
    assert err_field(get(client, fam, f"from=2026-10-20&to=2026-10-23&{q}")) == field


def test_range_limits(client, fam):  # noqa: F811
    # 검색 없음: 지금처럼 31일
    assert get(client, fam, "from=2026-09-23&to=2026-10-23").status_code == 200
    assert err_field(get(client, fam, "from=2026-09-22&to=2026-10-23")) == "to"
    # order 만으로는 늘어나지 않는다
    assert err_field(get(client, fam, "from=2026-09-22&to=2026-10-23&order=desc")) == "to"
    # filter 나 reason 이 있으면 366일 (양 끝 포함)
    for q in ("filter=taken", "reason=unclear", "filter=exceptions&reason=no_response"):
        assert get(client, fam, f"from=2025-10-23&to=2026-10-23&{q}").status_code == 200, q
        assert err_field(get(client, fam, f"from=2025-10-22&to=2026-10-23&{q}")) == "to", q
    assert err_field(get(client, fam, "from=2026-10-23&to=2026-10-22&filter=taken")) == "from"


def test_filters_over_a_year(client, hist):
    f, info = hist
    all_ids = [i for i, _, _ in info]  # 만든 순서 = scheduled_at 오래된 순

    def expect(pred):
        return [i for i, outcome, reasons in info if pred(outcome, reasons)]

    cases = {
        "filter=exceptions": expect(lambda o, r: bool(r) or o == "not_taken"),
        "filter=handoff": expect(lambda o, r: bool(r)),
        "filter=not_taken": expect(lambda o, r: o == "not_taken"),
        "filter=taken": expect(lambda o, r: o == "taken"),
        "reason=unclear": expect(lambda o, r: "unclear" in r),
        "reason=refill_unconfirmed": expect(lambda o, r: "refill_unconfirmed" in r),
        "reason=double_suspected": [],
        "filter=taken&reason=refill_unconfirmed": expect(
            lambda o, r: o == "taken" and "refill_unconfirmed" in r
        ),
        "filter=not_taken&reason=no_response": expect(
            lambda o, r: o == "not_taken" and "no_response" in r
        ),
    }
    for q, want in cases.items():
        got = ids(get(client, f, f"{YEAR}&{q}"))
        assert got == want, q
        assert ids(get(client, f, f"{YEAR}&{q}&order=desc")) == want[::-1], q
        assert ids(get(client, f, f"{YEAR}&{q}&order=asc")) == want, q
    # 규칙이 모든 경우를 실제로 만든다
    assert all(cases[q] for q in cases if q != "reason=double_suspected")
    open_ = expect(lambda o, r: o is None)
    assert open_ and set(open_) <= set(cases["filter=handoff"])
    assert len(cases["filter=taken"]) + len(cases["filter=not_taken"]) + len(open_) == len(all_ids)
    # 인계 없이 not_taken 인 건과 인계 있는 taken 건이 exceptions 에 들어간다
    exc = set(cases["filter=exceptions"])
    assert exc == set(cases["filter=handoff"]) | set(cases["filter=not_taken"])
    assert exc - set(cases["filter=handoff"]) and exc - set(cases["filter=not_taken"])


def test_no_query_keeps_old_behavior(client, hist):
    f, info = hist
    month = [i for i, _, _ in info][-60:]
    res = get(client, f, "from=2026-09-23&to=2026-10-22")
    body = res.json()
    assert set(body) == {"from", "to", "items"}
    assert body["from"] == "2026-09-23" and body["to"] == "2026-10-22"
    got = ids(res)
    assert got == [i for i in month if i in set(got)] and len(got) == 60
    assert ids(get(client, f, "from=2026-09-23&to=2026-10-22&order=desc")) == got[::-1]


def test_batch_serializer_matches_single(client, hist):
    f, _ = hist
    settings = client.app.state.settings
    with client.app.state.sessionmaker() as db:
        events = list(
            db.scalars(
                select(DoseEvent)
                .where(DoseEvent.family_id == f.family_id, DoseEvent.date >= "2026-09-01")
                .order_by(DoseEvent.scheduled_at)
            )
        )
        # 보관 기한으로 지워진 사진도 latest_reading 에는 나온다
        p = db.scalar(select(Photo).where(Photo.dose_event_id == events[0].id))
        p.deleted_at = events[0].scheduled_at
        db.commit()
    for guardian in (True, False):
        with client.app.state.sessionmaker() as db:
            batch = events_out(db, settings, "https://t", events, guardian=guardian)
        with client.app.state.sessionmaker() as db:
            single = [event_out(db, settings, "https://t", e, guardian=guardian) for e in events]
        assert batch == single
    assert batch[0]["photos"] == []
    with client.app.state.sessionmaker() as db:
        assert events_out(db, settings, "https://t", [], guardian=True) == []


# ---------- 요약 ----------


def _old_summarize(db, family_id, period, start, today, refill) -> dict:
    """R-051 이전 구현 (기간마다 쿼리). 새 구현과 결과가 같은지 비교용."""
    end = start if period == "day" else start + timedelta(days=6)
    events = db.scalars(
        select(DoseEvent).where(
            DoseEvent.family_id == family_id,
            DoseEvent.date >= start.isoformat(),
            DoseEvent.date <= end.isoformat(),
        )
    ).all()
    eids = [e.id for e in events]
    handoffs = db.scalars(select(Handoff).where(Handoff.dose_event_id.in_(eids))).all()
    readings = db.scalars(
        select(Reading).where(Reading.dose_event_id.in_(eids), Reading.kind == "cells")
    ).all()
    counts = {
        "scheduled": len(events),
        "taken_ai": sum(e.outcome == "taken" and e.outcome_source == "ai" for e in events),
        "taken_guardian": sum(
            e.outcome == "taken" and e.outcome_source == "guardian" for e in events
        ),
        "not_taken": sum(e.outcome == "not_taken" for e in events),
        "acknowledged": sum(e.outcome == "acknowledged" for e in events),
        "open_handoffs": sum(h.verdict is None for h in handoffs),
        "pending": sum(e.state in PENDING for e in events),
    }
    by_reason = Counter(h.reason for h in handoffs)
    return {
        "period": period,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "final": end < today,
        "counts": counts,
        "handoffs_by_reason": {r: by_reason.get(r, 0) for r in ALLOWED_VERDICTS},
        "all_normal": bool(events)
        and counts["taken_ai"] == len(events)
        and counts["open_handoffs"] == 0,
        "refill": refill,
        "includes_mock": any(r.is_mock for r in readings),
        "includes_simulated": any(e.is_simulated for e in events),
        "pillbox_mismatch": any(mismatch_cells(r) for r in readings if r.cells),
    }


def _summ(client, f, q: str):
    return client.get(f"{API}/summaries?{q}", headers=f.g)


def test_day_summaries_366_days_match_old(client, hist):
    f, _ = hist
    res = _summ(client, f, "period=day&from=2025-10-23&to=2026-10-23")
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 366
    assert items[0]["period_start"] == "2025-10-23" and items[-1]["period_start"] == "2026-10-23"
    assert err_field(_summ(client, f, "period=day&from=2025-10-22&to=2026-10-23")) == "to"
    today = date(2026, 10, 23)
    old = []
    with client.app.state.sessionmaker() as db:
        for i in range(366):
            d = START + timedelta(days=i)
            old.append(_old_summarize(db, f.family_id, "day", d, today, items[i]["refill"]))
    assert items == old
    assert any(i["pillbox_mismatch"] for i in items) and any(i["all_normal"] for i in items)
    assert any(i["handoffs_by_reason"]["refill_unconfirmed"] for i in items)
    weeks = _summ(client, f, "period=week&from=2026-08-01&to=2026-10-23").json()["items"]
    with client.app.state.sessionmaker() as db:
        assert weeks == [
            _old_summarize(
                db, f.family_id, "week", date.fromisoformat(w["period_start"]), today, w["refill"]
            )
            for w in weeks
        ]
    assert err_field(_summ(client, f, "period=week&from=2026-07-01&to=2026-10-23")) == "to"


def test_total_summary_with_history(client, hist):
    f, info = hist
    items = _summ(client, f, "period=total&from=2026-10-23&to=2026-10-01").json()["items"]
    assert len(items) == 1
    t = items[0]
    assert t["period"] == "total" and t["final"] is False
    assert t["period_start"] == "2025-10-23" and t["period_end"] == "2026-10-23"
    days = _summ(client, f, "period=day&from=2025-10-23&to=2026-10-23").json()["items"]
    assert t["counts"] == {k: sum(d["counts"][k] for d in days) for k in t["counts"]}
    assert t["counts"]["scheduled"] == len(info) + 2  # 오늘 2건 (아직 전)
    assert t["counts"]["pending"] == 2 and t["counts"]["open_handoffs"] > 0
    for r in ALLOWED_VERDICTS:
        assert t["handoffs_by_reason"][r] == sum(d["handoffs_by_reason"][r] for d in days)
    assert t["all_normal"] is False
    assert t["includes_mock"] is True and t["includes_simulated"] is False
    assert t["pillbox_mismatch"] is True
    assert t["refill"] == days[-1]["refill"]


def test_total_summary_empty_family(client):
    f = Family(client)
    res = _summ(client, f, "period=total")
    assert res.status_code == 200, res.text
    (t,) = res.json()["items"]
    assert t["period_start"] == t["period_end"] == "2026-10-23"
    assert t["final"] is False and t["all_normal"] is False
    assert set(t["counts"].values()) == {0} and set(t["handoffs_by_reason"].values()) == {0}
    assert t["refill"] is None and not t["includes_mock"] and not t["pillbox_mismatch"]
    assert _summ(client, f, "period=month").status_code == 422
    assert client.get(f"{API}/summaries?period=total", headers=f.e).status_code == 403


def test_total_summary_ignores_future_and_other_families(client, fam):  # noqa: F811
    other = Family(client)
    other.register_pillbox()
    other.put_schedule(week_schedule())
    with client.app.state.sessionmaker() as db:
        seed_history(db, other.family_id, date(2026, 1, 1), date(2026, 1, 3))
        seed_history(db, fam.family_id, date(2026, 10, 20), date(2026, 10, 22))
        seed_history(db, fam.family_id, date(2026, 10, 24), date(2026, 10, 30))
    (t,) = _summ(client, fam, "period=total").json()["items"]
    assert t["period_start"] == "2026-10-20" and t["period_end"] == "2026-10-23"
    assert t["counts"]["scheduled"] == 8  # 10/20~10/23, 하루 2건
