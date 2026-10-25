"""QA 이슈 수정 (R-021): Q-001, Q-004, Q-005, Q-006, Q-007.

시각: 2026-10-23 = 금요일(요일 4). 일정 칸은 r{행}c{요일}.
"""

from app.seed_demo import seed
from tests.conftest import Family, bearer, week_schedule
from tests.test_dose import at, event_id, fam, jobs, open_handoffs, today, upload  # noqa: F401


def _family_with_pillbox(client) -> Family:
    f = Family(client)
    f.register_pillbox()
    return f


# ---------- Q-001 일정 저장 시각보다 이른 오늘 칸은 건을 만들지 않는다 ----------


def test_q001_schedule_saved_after_dose_times_creates_no_event_today(client, push):
    at(23, 20, 0)  # 아침 08:00, 저녁 19:00 이 이미 지난 뒤 저장
    f = _family_with_pillbox(client)
    f.put_schedule(week_schedule())
    assert today(client, f.g)["items"] == []
    at(23, 21, 0)
    assert open_handoffs(client, f) == []
    assert [m for m in push.sent if m.data.get("type") == "handoff"] == []
    # 다음 날부터는 평소대로
    at(24, 7, 0)
    assert [e["slot"]["time_of_day"] for e in today(client, f.g)["items"]] == ["morning", "evening"]


def test_q001_only_earlier_slots_are_skipped(client):
    at(23, 12, 0)
    f = _family_with_pillbox(client)
    f.put_schedule(week_schedule())
    assert [e["slot"]["time_of_day"] for e in today(client, f.g)["items"]] == ["evening"]


def test_q001_reschedule_does_not_recreate_past_events(client, fam, reader):  # noqa: F811
    at(23, 8, 5)
    reader.states = {"r0c4": "empty"}
    assert upload(client, fam, event_id(client, fam, "morning")).json()["result"] == "recorded"
    at(23, 12, 0)
    fam.put_schedule(week_schedule((("morning", "08:00"), ("evening", "20:00"))))
    items = today(client, fam.g)["items"]
    # 아침 건은 이미 기록된 원래 건 하나만, 저녁은 새 시각
    assert [(e["slot"]["time_of_day"], e["state"]) for e in items] == [
        ("morning", "done"),
        ("evening", "scheduled"),
    ]
    assert items[1]["scheduled_at"] == "2026-10-23T20:00:00+09:00"


def test_q001_jobs_after_restart_still_create_earlier_slots(client):
    """재시작 뒤 늦게 도는 예약 작업은 전날 이전에 만든 칸이면 그날 건을 만든다."""
    f = _family_with_pillbox(client)
    f.put_schedule(week_schedule())  # 금 07:00 에 저장
    at(24, 9, 0)  # 토요일 09:00 에 서버가 처음 돈 것처럼
    jobs(client)
    items = today(client, f.g)["items"]
    today_items = [e for e in items if not e["carried_over"]]
    assert [e["slot"]["time_of_day"] for e in today_items] == ["morning", "evening"]
    sat = [h for h in open_handoffs(client, f) if h["dose_event"]["date"] == "2026-10-24"]
    assert [h["reason"] for h in sat] == ["no_response"]  # 토 08:00 건은 무응답 인계


def test_q001_demo_seed_late_in_day_has_no_false_handoffs(client, settings, push):
    settings.demo_mode = True
    at(23, 20, 30)
    out = seed(client)
    g, e = bearer(out["guardian_token"]), bearer(out["elder_token"])
    at(23, 21, 30)
    jobs(client)
    assert client.get("/api/v1/handoffs", headers=g).json()["items"] == []
    # 시연 트리거는 오늘 건이 없는 칸도 연다
    slots = client.get("/api/v1/schedule", headers=g).json()["slots"]
    fri_pm = next(s for s in slots if s["weekday"] == 4 and s["time_of_day"] == "evening")
    res = client.post("/api/v1/dev/dose-events", json={"slot_id": fri_pm["id"]}, headers=e)
    assert res.status_code == 201 and res.json()["is_simulated"] is True


# ---------- Q-005 다시 찍어 달라는 요청은 건당 1회 ----------


def test_q005_unclear_then_unchanged_hands_off(client, fam, reader):  # noqa: F811
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "retake"
    reader.states = {}  # 칸 그대로
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unchanged"]


def test_q005_unchanged_then_unclear_hands_off(client, fam, reader):  # noqa: F811
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    assert upload(client, fam, eid).json()["result"] == "remind_retake"
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unclear"]


# ---------- Q-004 늦은 정상 사진은 어르신 기준 recorded ----------


def test_q004_late_normal_photo_is_recorded_for_elder(client, fam, reader, push):  # noqa: F811
    at(23, 8, 31)
    eid = event_id(client, fam, "morning")
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["no_response"]
    at(23, 9, 0)
    reader.states = {"r0c4": "empty"}
    res = upload(client, fam, eid, taken_at="2026-10-23T08:58:00+09:00").json()
    assert res["result"] == "recorded"
    ev = res["dose_event"]
    assert ev["elder_status"] == "recorded" and ev["taken_at"] == "2026-10-23T08:58:00+09:00"
    assert ev["state"] == "handed_off" and ev["outcome"] is None
    elder = today(client, fam.e)["items"][0]
    assert elder["elder_status"] == "recorded"
    # 보호자 인계는 열린 채, late_photo, 다시 알리지 않음
    hs = open_handoffs(client, fam)
    assert hs[0]["late_photo"] is True and len(push.sent) == 1
    # 보호자가 "확인했어요"여도 어르신 기록은 남는다
    client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "acknowledged"}, headers=fam.g
    )
    elder = today(client, fam.e)["items"][0]
    assert elder["elder_status"] == "recorded"
    assert elder["taken_at"] == "2026-10-23T08:58:00+09:00"


def test_q004_guardian_not_taken_clears_record(client, fam, reader):  # noqa: F811
    at(23, 8, 31)
    eid = event_id(client, fam, "morning")
    at(23, 9, 0)
    reader.states = {"r0c4": "empty"}
    upload(client, fam, eid)
    h = open_handoffs(client, fam)[0]
    client.post(f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "not_taken"}, headers=fam.g)
    elder = today(client, fam.e)["items"][0]
    assert elder["elder_status"] == "no_record" and elder["taken_at"] is None


def test_q004_guardian_taken_keeps_photo_time(client, fam, reader):  # noqa: F811
    at(23, 8, 31)
    eid = event_id(client, fam, "morning")
    at(23, 9, 0)
    reader.states = {"r0c4": "empty"}
    upload(client, fam, eid)
    h = open_handoffs(client, fam)[0]
    at(23, 10, 0)
    res = client.post(
        f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "taken"}, headers=fam.g
    ).json()
    assert res["dose_event"]["taken_at"] == "2026-10-23T09:00:00+09:00"


def test_q004_late_unclear_photo_stays_no_record(client, fam, reader):  # noqa: F811
    at(23, 8, 31)
    eid = event_id(client, fam, "morning")
    at(23, 9, 0)
    reader.states = {"r0c4": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert today(client, fam.e)["items"][0]["elder_status"] == "no_record"


# ---------- Q-006 reminded_at ----------


def test_q006_reminded_at_for_guardian_only(client, fam, reader):  # noqa: F811
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    res = upload(client, fam, eid).json()
    assert res["result"] == "remind_retake" and res["dose_event"]["reminded_at"] is None
    g = today(client, fam.g)["items"][0]
    assert g["reminded_at"] == "2026-10-23T08:05:00+09:00"
    assert today(client, fam.e)["items"][0]["reminded_at"] is None
    at(23, 8, 21)  # 재촬영 기한 넘김 → unchanged 인계, 안내 사실은 남는다
    h = open_handoffs(client, fam)[0]
    assert h["reason"] == "unchanged"
    detail = client.get(f"/api/v1/handoffs/{h['id']}", headers=fam.g).json()
    assert detail["dose_event"]["reminded_at"] == "2026-10-23T08:05:00+09:00"


def test_q006_not_reminded_is_null(client, fam):  # noqa: F811
    assert today(client, fam.g)["items"][0]["reminded_at"] is None


# ---------- Q-007 전날 끝나지 않은 건은 6시간 안이면 오늘 목록에 ----------


BEDTIME = (("morning", "08:00"), ("bedtime", "23:00"))


def _bedtime_family(client) -> Family:
    f = _family_with_pillbox(client)
    f.put_schedule(week_schedule(BEDTIME))  # 금 07:00 저장 → 금 23:00 건 있음
    return f


def test_q007_missed_bedtime_carries_over_and_can_be_photographed(client, reader):
    f = _bedtime_family(client)
    at(23, 23, 31)
    hs = open_handoffs(client, f)
    assert [h["dose_event"]["slot"]["time_of_day"] for h in hs].count("bedtime") == 1
    at(24, 0, 30)
    items = today(client, f.e)["items"]
    carried = [e for e in items if e["carried_over"]]
    assert len(carried) == 1 and carried[0]["date"] == "2026-10-23"
    assert carried[0]["slot"]["time_of_day"] == "bedtime" and items[0] is carried[0]
    assert all(not e["carried_over"] for e in items[1:])
    # 자정 뒤에 찍어도 받는다 (늦은 정상 사진 → 어르신 기준 기록)
    reader.states = {"r1c4": "empty"}
    assert upload(client, f, carried[0]["id"]).json()["result"] == "recorded"
    after = [e for e in today(client, f.e)["items"] if e["carried_over"]]
    assert len(after) == 1 and after[0]["elder_status"] == "recorded"


def test_q007_open_event_crossing_midnight_carries_over(client, reader):
    f = Family(client)
    f.register_pillbox()
    f.put_schedule(week_schedule((("morning", "08:00"), ("bedtime", "23:50"))))
    at(24, 0, 5)  # 23:50 건은 아직 asked (기한 00:20)
    items = today(client, f.e)["items"]
    assert [(e["carried_over"], e["state"]) for e in items if e["carried_over"]] == [
        (True, "asked")
    ]


def test_q007_not_carried_after_six_hours_or_when_finished(client, reader):
    f = _bedtime_family(client)
    at(23, 23, 31)
    jobs(client)
    at(24, 5, 1)  # 23:00 + 6시간 지남
    assert [e for e in today(client, f.e)["items"] if e["carried_over"]] == []


def test_q007_handed_off_with_photo_before_midnight_not_carried(client, reader):
    f = _bedtime_family(client)
    at(23, 23, 5)
    reader.states = {"r1c4": "empty", "r0c5": "empty"}  # 이중 복용 의심 → 사진 있는 인계
    upload(client, f, event_id(client, f, "bedtime"))
    at(24, 0, 30)
    assert [e for e in today(client, f.e)["items"] if e["carried_over"]] == []
