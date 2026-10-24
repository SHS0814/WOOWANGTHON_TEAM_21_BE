"""복약 건·사진 업로드·분류·인계·판정 (backend.md 6·7장, R-010, R-014).

시각: T0 = 2026-10-23(금) 07:00 KST. 일정: 아침 08:00 = r0c{요일}, 저녁 19:00 = r1c{요일}.
금요일은 요일 4 → 아침 r0c4, 저녁 r1c4.
"""

from datetime import datetime

import pytest

from app.clock import clock
from app.timeutil import KST
from tests.conftest import SAMPLE_PNG, Family, week_schedule


def at(day: int, hh: int, mm: int = 0) -> None:
    clock.set(datetime(2026, 10, day, hh, mm, tzinfo=KST))


@pytest.fixture
def fam(client, reader):
    f = Family(client)
    f.register_pillbox()  # FakeReader 기본값 full → 기준선 전부 full
    f.put_schedule(week_schedule())
    return f


def today(client, headers) -> dict:
    res = client.get("/api/v1/dose-events/today", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def event_id(client, f, tod: str) -> str:
    items = today(client, f.g)["items"]
    return next(e["id"] for e in items if e["slot"]["time_of_day"] == tod)


def upload(client, f, eid: str, **form):
    return client.post(
        f"/api/v1/dose-events/{eid}/photo",
        files={"photo": ("p.png", SAMPLE_PNG, "image/png")},
        data=form,
        headers=f.e,
    )


def open_handoffs(client, f) -> list[dict]:
    return client.get("/api/v1/handoffs", headers=f.g).json()["items"]


def take(client, f, reader, tod: str, empty: list[str]):
    """해당 시간대 복약 건을 정상으로 기록한다 (empty 칸은 비어 보임)."""
    reader.states = {c: "empty" for c in empty}
    res = upload(client, f, event_id(client, f, tod))
    assert res.json()["result"] == "recorded", res.json()
    return res


def test_today_events_elder_and_guardian_views(client, fam):
    elder = today(client, fam.e)
    assert elder["date"] == "2026-10-23"
    assert [e["slot"]["cell_id"] for e in elder["items"]] == ["r0c4", "r1c4"]
    morning = elder["items"][0]
    assert morning["state"] == "scheduled" and morning["elder_status"] == "upcoming"
    assert morning["ask_times"] == [
        "2026-10-23T08:00:00+09:00",
        "2026-10-23T08:10:00+09:00",
        "2026-10-23T08:20:00+09:00",
    ]
    assert morning["handoff_at"] == "2026-10-23T08:30:00+09:00"
    assert morning["is_simulated"] is False
    assert elder["open_handoff_count"] is None
    assert elder["next_event"]["time_of_day"] == "morning"
    guardian = today(client, fam.g)
    assert guardian["open_handoff_count"] == 0
    at(23, 8, 1)
    assert today(client, fam.e)["items"][0]["state"] == "asked"


def test_early_photo_only_within_60_minutes(client, fam, reader):
    at(23, 6, 59)
    eid = event_id(client, fam, "morning")
    res = upload(client, fam, eid)
    assert res.status_code == 409 and res.json()["error"]["code"] == "event_not_open"
    at(23, 7, 0)
    reader.states = {"r0c4": "empty"}
    assert upload(client, fam, eid).json()["result"] == "recorded"


def test_normal_is_recorded_without_push(client, fam, reader, push):
    at(23, 8, 5)
    res = take(client, fam, reader, "morning", ["r0c4"])
    body = res.json()
    assert body["is_mock"] is True
    ev = body["dose_event"]
    assert ev["state"] == "done" and ev["outcome"] == "taken" and ev["outcome_source"] == "ai"
    assert ev["elder_status"] == "recorded" and ev["taken_at"] == "2026-10-23T08:05:00+09:00"
    assert ev["latest_reading"] is None and len(ev["photos"]) == 1
    assert push.sent == []
    g = today(client, fam.g)["items"][0]
    cells = {c["cell_id"]: c for c in g["latest_reading"]["cells"]}
    assert cells["r0c4"]["expected"] == "empty" and cells["r0c4"]["attention"] is None
    assert g["latest_reading"]["classification"] == "normal"


def test_partial_mismatch_without_refill_suspicion(client, fam, reader, push):
    # 기준선에서 월요일 두 칸이 비어 있었다고 보호자가 고침
    cur = client.get("/api/v1/pillboxes/current", headers=fam.g).json()
    client.put(
        f"/api/v1/refills/{cur['last_refill']['id']}/baseline",
        json={
            "cells": [{"cell_id": "r0c0", "state": "empty"}, {"cell_id": "r1c0", "state": "empty"}]
        },
        headers=fam.g,
    )
    at(23, 8, 5)
    # r0c0 은 비어 있어야 하는데 차 보임, r1c0 은 비어 보임 → 일부 불일치
    reader.states = {"r0c4": "empty", "r0c0": "full", "r1c0": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning"))
    assert res.json()["result"] == "recorded"
    assert open_handoffs(client, fam) == [] and push.sent == []
    g = today(client, fam.g)["items"][0]
    cells = {c["cell_id"]: c for c in g["latest_reading"]["cells"]}
    assert cells["r0c0"]["attention"] == "unexpected_full"
    mm = client.get("/api/v1/pillboxes/current", headers=fam.g).json()["pillbox_mismatch"]
    assert mm["detected"] is True and mm["cell_ids"] == ["r0c0"]


def test_double_suspected_hands_off(client, fam, reader, push):
    at(23, 8, 5)
    reader.states = {"r0c4": "empty", "r0c5": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning"))
    assert res.json()["result"] == "guardian_notified"
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["double_suspected"]
    assert hs[0]["allowed_verdicts"] == ["taken", "not_taken", "acknowledged"]
    assert [m.data["reason"] for m in push.sent] == ["double_suspected"]
    detail = client.get(f"/api/v1/handoffs/{hs[0]['id']}", headers=fam.g).json()
    cells = {c["cell_id"]: c for c in detail["readings"][0]["cells"]}
    assert cells["r0c5"]["attention"] == "unexpected_empty"
    assert all("confidence" not in c for c in detail["readings"][0]["cells"])
    assert detail["layout"]["rows"] == 2
    # 어르신 화면에는 인계 사유가 보이지 않는다
    elder = today(client, fam.e)["items"][0]
    assert elder["elder_status"] == "no_record" and elder["handoff"] is None


def test_unclear_retake_then_handoff(client, fam, reader, push):
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    reader.states = {"r0c4": "unclear"}
    res = upload(client, fam, eid).json()
    assert res["result"] == "retake"
    assert res["dose_event"]["state"] == "retake_requested"
    assert res["dose_event"]["retake_deadline_at"] == "2026-10-23T08:20:00+09:00"
    # 낮은 신뢰도도 애매
    reader.states = {"r0c4": "empty"}
    reader.confidence = {"r0c4": 0.5}
    at(23, 8, 7)
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["unclear"] and hs[0]["allowed_verdicts"] == [
        "taken",
        "not_taken",
    ]
    detail = client.get(f"/api/v1/handoffs/{hs[0]['id']}", headers=fam.g).json()
    assert len(detail["readings"]) == 2
    assert detail["readings"][1]["cells"][0]["cell_id"] == "r0c0"
    target = next(c for c in detail["readings"][1]["cells"] if c["cell_id"] == "r0c4")
    assert target["attention"] == "low_confidence"


def test_retake_deadline_hands_off(client, fam, reader, push):
    at(23, 8, 5)
    reader.states = {"r0c4": "unclear"}
    upload(client, fam, event_id(client, fam, "morning"))
    at(23, 8, 21)
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unclear"]


def test_reader_failure_is_unclear(client, fam, reader):
    at(23, 8, 5)
    reader.fail = True
    res = upload(client, fam, event_id(client, fam, "morning")).json()
    assert res["result"] == "retake"


def test_unchanged_reminds_once_then_hands_off(client, fam, reader, push):
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    res = upload(client, fam, eid).json()  # 전부 full → 칸 그대로
    assert res["result"] == "remind_retake" and res["dose_event"]["state"] == "reminded"
    at(23, 8, 10)
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unchanged"]


def test_unchanged_reminder_timeout(client, fam, reader):
    at(23, 8, 25)
    upload(client, fam, event_id(client, fam, "morning"))
    # 안내가 +30분 안이면 재촬영 기한은 그 뒤로 넘어가도 된다 (R-010 6번)
    at(23, 8, 35)
    assert open_handoffs(client, fam) == []
    at(23, 8, 40)
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unchanged"]


def test_unchanged_handoff_immediately_setting(client, fam, reader):
    client.put("/api/v1/settings", json={"unchanged_mode": "handoff_immediately"}, headers=fam.g)
    at(23, 8, 5)
    assert upload(client, fam, event_id(client, fam, "morning")).json()["result"] == (
        "guardian_notified"
    )


def test_late_unchanged_skips_reminder(client, fam, reader):
    """C3: +30분 지난 사진은 안내 없이 바로 인계."""
    at(23, 8, 20)
    eid = event_id(client, fam, "morning")
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "retake"
    at(23, 8, 33)
    reader.states = {}
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unchanged"]


def test_no_response_then_late_photo(client, fam, reader, push):
    at(23, 8, 30)
    eid = event_id(client, fam, "morning")
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["no_response"]
    assert len(push.sent) == 1
    at(23, 9, 0)
    reader.states = {"r0c4": "empty"}
    res = upload(client, fam, eid).json()
    assert res["result"] == "recorded"
    assert res["dose_event"]["state"] == "handed_off"  # 인계는 열린 채 (R-010 4번)
    hs = open_handoffs(client, fam)
    assert hs[0]["late_photo"] is True and len(push.sent) == 1
    # 판정
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "taken"}, headers=fam.g
    )
    assert res.status_code == 200
    ev = res.json()["dose_event"]
    assert ev["state"] == "done" and ev["outcome_source"] == "guardian"
    assert ev["decided_by"]["display_name"] == "민수"
    assert today(client, fam.e)["items"][0]["elder_status"] == "recorded"
    # 이미 판정된 건
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "not_taken"}, headers=fam.g
    )
    assert res.status_code == 409 and res.json()["error"]["details"]["verdict"] == "taken"
    # 닫힌 인계 사진은 받지 않는다
    assert upload(client, fam, eid).json()["error"]["code"] == "event_not_open"


def test_verdict_not_allowed(client, fam, reader):
    at(23, 8, 5)
    reader.states = {"r0c4": "unclear"}
    eid = event_id(client, fam, "morning")
    upload(client, fam, eid)
    upload(client, fam, eid)
    h = open_handoffs(client, fam)[0]
    res = client.post(
        f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "acknowledged"}, headers=fam.g
    )
    assert res.status_code == 422 and res.json()["error"]["code"] == "verdict_not_allowed"


def test_missed_cell_is_ignored_later(client, fam, reader, push):
    """C2: 인계된 칸은 이후 비교에서 제외 (늦게 비워도 이중 복용 아님)."""
    at(23, 8, 30)
    hs = open_handoffs(client, fam)
    client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "not_taken"}, headers=fam.g
    )
    at(23, 19, 5)
    reader.states = {"r1c4": "empty", "r0c4": "empty"}
    res = upload(client, fam, event_id(client, fam, "evening")).json()
    assert res["result"] == "recorded"
    g = today(client, fam.g)["items"][1]
    cells = {c["cell_id"]: c for c in g["latest_reading"]["cells"]}
    assert cells["r0c4"]["expected"] == "ignored" and cells["r0c4"]["attention"] is None


def test_refill_suspected_creates_one_check(client, fam, reader, push):
    """C1: 리필 기록 없이 대상 칸 외 전부 full → 확인 요청 1회, 복약은 정상."""
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    at(23, 19, 5)
    reader.states = {"r1c4": "empty"}  # r0c4 가 다시 차 보임 = 채운 것 같음
    res = upload(client, fam, event_id(client, fam, "evening")).json()
    assert res["result"] == "recorded"
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["refill_unconfirmed"]
    assert hs[0]["allowed_verdicts"] == ["refilled", "not_refilled"]
    assert hs[0]["dose_event"]["state"] == "done"  # 묶이지 않음
    assert [m.data["reason"] for m in push.sent] == ["refill_unconfirmed"]
    # 다음 날 같은 상황이어도 열려 있으면 다시 만들지 않음
    at(24, 8, 5)
    reader.states = {"r0c5": "empty"}
    upload(client, fam, event_id(client, fam, "morning"))
    assert len(open_handoffs(client, fam)) == 1 and len(push.sent) == 1
    # "네" → 그 사진이 새 기준선
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "refilled"}, headers=fam.g
    )
    assert res.status_code == 200
    cur = client.get("/api/v1/pillboxes/current", headers=fam.g).json()
    assert cur["last_refill"]["source"] == "refill_check"


def test_refill_not_refilled_ignores_full_cells(client, fam, reader, push):
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    at(23, 19, 5)
    reader.states = {"r1c4": "empty"}
    upload(client, fam, event_id(client, fam, "evening"))
    h = open_handoffs(client, fam)[0]
    client.post(
        f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "not_refilled"}, headers=fam.g
    )
    at(24, 8, 5)
    reader.states = {"r0c5": "empty", "r1c4": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning")).json()
    assert res["result"] == "recorded"
    assert open_handoffs(client, fam) == []  # 같은 원인으로 다시 묻지 않음 (R-010 3번)
    g = today(client, fam.g)["items"][0]
    cells = {c["cell_id"]: c for c in g["latest_reading"]["cells"]}
    assert cells["r0c4"]["expected"] == "ignored"


# ---------- R-014: 이번 주기에 이미 쓴 칸이 다시 대상 칸 ----------


def _use_friday(client, fam, reader):
    """1주차 금요일 아침·저녁을 모두 정상 복용 처리하고 다음 주 금요일 아침으로 간다."""
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    at(23, 19, 5)
    take(client, fam, reader, "evening", ["r0c4", "r1c4"])
    at(30, 8, 5)  # 리필 없이 일주일 뒤 금요일


def test_already_used_cell_empty_is_never_recorded_refill_overdue(client, fam, reader, push):
    _use_friday(client, fam, reader)
    # 대상 칸(r0c4)은 이미 쓴 칸이라 비어 보임. 다른 칸도 일부 비어 있음 → 약통이 안 채워짐
    reader.states = {"r0c4": "empty", "r1c4": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning")).json()
    assert res["result"] == "guardian_notified"
    assert res["dose_event"]["outcome"] is None and res["dose_event"]["elder_status"] == "no_record"
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["refill_overdue"]
    assert hs[0]["allowed_verdicts"] == ["taken", "not_taken", "acknowledged"]
    assert push.sent[-1].data["reason"] == "refill_overdue"


def test_already_used_cell_with_refill_suspicion_bound_to_check(client, fam, reader, push):
    _use_friday(client, fam, reader)
    # 대상 칸만 비고 나머지 다 차 있음 → 보호자가 채운 것 같음
    reader.states = {"r0c4": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning")).json()
    assert res["result"] == "guardian_notified"  # recorded 아님 (원칙 5)
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["refill_unconfirmed"]
    assert hs[0]["dose_event"]["state"] == "handed_off"
    assert hs[0]["dose_event"]["handoff"]["id"] == hs[0]["id"]
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "refilled"}, headers=fam.g
    )
    ev = res.json()["dose_event"]
    assert ev["outcome"] == "taken" and ev["outcome_source"] == "guardian"
    cur = client.get("/api/v1/pillboxes/current", headers=fam.g).json()
    assert cur["last_refill"]["source"] == "refill_check"


def test_already_used_not_refilled_waits_for_verdict(client, fam, reader, push):
    _use_friday(client, fam, reader)
    reader.states = {"r0c4": "empty"}
    eid = event_id(client, fam, "morning")
    upload(client, fam, eid)
    h = open_handoffs(client, fam)[0]
    n_push = len(push.sent)
    client.post(
        f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "not_refilled"}, headers=fam.g
    )
    hs = open_handoffs(client, fam)
    assert [x["reason"] for x in hs] == ["refill_overdue"]
    assert hs[0]["dose_event"]["state"] == "handed_off" and len(push.sent) == n_push
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "not_taken"}, headers=fam.g
    )
    assert res.json()["dose_event"]["outcome"] == "not_taken"


def test_already_used_low_confidence_or_retake_never_recorded(client, fam, reader):
    _use_friday(client, fam, reader)
    eid = event_id(client, fam, "morning")
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "retake"
    reader.states = {"r0c4": "empty", "r1c4": "empty"}
    res = upload(client, fam, eid).json()
    assert res["result"] == "guardian_notified" and res["dose_event"]["outcome"] is None


def test_daily_limit_on_elder_upload_hands_off(client, fam, reader, settings):
    settings.daily_reading_limit = 1  # 약통 등록에서 이미 1회 씀
    at(23, 8, 5)
    res = upload(client, fam, event_id(client, fam, "morning")).json()
    assert res["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unclear"]


def test_client_upload_id_is_idempotent(client, fam, reader):
    at(23, 8, 5)
    reader.states = {"r0c4": "empty"}
    eid = event_id(client, fam, "morning")
    a = upload(client, fam, eid, client_upload_id="u1").json()
    b = upload(client, fam, eid, client_upload_id="u1").json()
    assert a["result"] == b["result"] == "recorded"
    assert len(b["dose_event"]["photos"]) == 1


def test_upload_permissions(client, fam):
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    res = client.post(
        f"/api/v1/dose-events/{eid}/photo",
        files={"photo": ("p.png", SAMPLE_PNG, "image/png")},
        headers=fam.g,
    )
    assert res.status_code == 403
    other = Family(client)
    res = client.post(
        f"/api/v1/dose-events/{eid}/photo",
        files={"photo": ("p.png", SAMPLE_PNG, "image/png")},
        headers=other.e,
    )
    assert res.status_code == 404


def test_dose_events_range(client, fam, reader):
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    res = client.get("/api/v1/dose-events?from=2026-10-20&to=2026-10-23", headers=fam.g).json()
    assert len(res["items"]) == 2
    res = client.get("/api/v1/dose-events?from=2026-09-01&to=2026-10-23", headers=fam.g)
    assert res.status_code == 422
    assert (
        client.get("/api/v1/dose-events?from=2026-10-20&to=2026-10-23", headers=fam.e).status_code
        == 403
    )


def test_dev_trigger_requires_demo(client, fam):
    slot = client.get("/api/v1/schedule", headers=fam.g).json()["slots"]
    fri = next(s for s in slot if s["weekday"] == 4 and s["time_of_day"] == "evening")
    res = client.post("/api/v1/dev/dose-events", json={"slot_id": fri["id"]}, headers=fam.e)
    assert res.status_code == 404


def test_dev_trigger_in_demo_family(client, settings, reader):
    settings.demo_mode = True
    f = Family(client, is_demo=True)
    f.register_pillbox()
    f.put_schedule(week_schedule())
    slots = client.get("/api/v1/schedule", headers=f.g).json()["slots"]
    fri = next(s for s in slots if s["weekday"] == 4 and s["time_of_day"] == "evening")
    sat = next(s for s in slots if s["weekday"] == 5)
    at(23, 10, 0)
    res = client.post("/api/v1/dev/dose-events", json={"slot_id": fri["id"]}, headers=f.e)
    assert res.status_code == 201, res.text
    ev = res.json()
    assert ev["is_simulated"] is True and ev["state"] == "asked"
    assert ev["scheduled_at"] == "2026-10-23T10:00:00+09:00"
    # 실제 판독 흐름을 탄다
    reader.states = {"r1c4": "empty"}
    assert upload(client, f, ev["id"]).json()["result"] == "recorded"
    res = client.post("/api/v1/dev/dose-events", json={"slot_id": fri["id"]}, headers=f.e)
    assert res.status_code == 409 and res.json()["error"]["code"] == "event_already_started"
    res = client.post("/api/v1/dev/dose-events", json={"slot_id": sat["id"]}, headers=f.e)
    assert res.status_code == 422
    assert client.get("/api/v1/me", headers=f.e).json()["family"]["is_demo"] is True


def test_stuck_reading_state_recovers(client, fam, reader):
    from app.models import DoseEvent

    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "retake"
    with client.app.state.sessionmaker() as db:
        db.get(DoseEvent, eid).state = "reading"  # 판독 중 서버가 죽은 상황
        db.commit()
    res = upload(client, fam, eid)
    assert res.status_code == 409 and res.json()["error"]["code"] == "reading_in_progress"
    at(23, 8, 8)
    assert today(client, fam.e)["items"][0]["state"] == "asked"
