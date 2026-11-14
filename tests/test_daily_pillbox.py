"""하루 약통 (R-054): 아침·점심·저녁 3칸을 매일 같은 칸으로 쓰고 저녁 뒤 채운다.

시각: T0 = 2026-10-23(금) 07:00 KST. 약통 1×3 (r0c0 아침, r0c1 점심, r0c2 저녁), 매일 같은 칸.
"""

import pytest

from app.jobs import summary_body
from tests.conftest import SAMPLE_PNG, Family
from tests.test_dose import at, event_id, open_handoffs, take, upload

DAILY = (("morning", "08:00", "r0c0"), ("noon", "12:00", "r0c1"), ("evening", "19:00", "r0c2"))


def daily_schedule() -> list[dict]:
    return [
        {"weekday": wd, "time_of_day": tod, "dose_time": t, "cell_id": cell}
        for wd in range(7)
        for tod, t, cell in DAILY
    ]


@pytest.fixture
def day(client, reader):
    f = Family(client)
    f.register_pillbox(rows=1, cols=3)  # 기준선 전부 full (07:00)
    out = f.put_schedule(daily_schedule())
    assert len(out["slots"]) == 21
    return f


def status(client, f) -> dict:
    return client.get("/api/v1/pillboxes/current", headers=f.g).json()["refill_status"]


def summary(client, f, date: str) -> dict:
    res = client.get(f"/api/v1/summaries?period=day&from={date}&to={date}", headers=f.g)
    assert res.status_code == 200, res.text
    return res.json()["items"][0]


def refill(client, f, reader) -> dict:
    reader.states = {}  # 채운 약통: 전부 full
    res = client.post(
        "/api/v1/refills", files={"photo": ("box.png", SAMPLE_PNG, "image/png")}, headers=f.g
    )
    assert res.status_code == 201, res.text
    return res.json()


def covered(st) -> tuple[str, str] | None:
    c = st["covered_until"]
    return None if c is None else (c["date"], c["time_of_day"])


def use_friday(client, f, reader):
    at(23, 8, 5)
    take(client, f, reader, "morning", ["r0c0"])
    at(23, 12, 5)
    take(client, f, reader, "noon", ["r0c0", "r0c1"])
    at(23, 19, 5)
    take(client, f, reader, "evening", ["r0c0", "r0c1", "r0c2"])


def test_schedule_validation_same_cell_rules(client):
    f = Family(client)
    f.register_pillbox(rows=1, cols=3)
    slots = daily_schedule()
    # 같은 칸·같은 요일 두 번
    bad = [slots[0], {**slots[1], "cell_id": "r0c0"}]
    res = client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g)
    assert res.status_code == 422
    assert res.json()["error"]["details"]["fields"][0]["field"] == "slots.1.cell_id"
    # 같은 요일·시간대 두 번 (다른 칸)
    bad = [slots[0], {**slots[1], "time_of_day": "morning"}]
    res = client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g)
    assert res.status_code == 422
    assert res.json()["error"]["details"]["fields"][0]["field"] == "slots.1"
    # 같은 칸을 요일마다: 허용
    assert client.put("/api/v1/schedule", json={"slots": slots}, headers=f.g).status_code == 200


def test_daily_flow_record_refill_next_morning(client, day, reader, push):
    st = status(client, day)
    # 하루 약통은 한 번 채워 하루를 간다 → 기준 0칸 (저녁 뒤에만 채우라고 한다)
    assert st["threshold_cells"] == 0
    assert st["remaining_cells"] == 3 and st["needs_refill"] is False
    assert covered(st) == ("2026-10-23", "evening")

    at(23, 8, 5)
    take(client, day, reader, "morning", ["r0c0"])
    st = status(client, day)
    assert st["remaining_cells"] == 2 and st["needs_refill"] is False
    assert covered(st) == ("2026-10-23", "evening")
    at(23, 12, 5)
    take(client, day, reader, "noon", ["r0c0", "r0c1"])
    assert status(client, day)["remaining_cells"] == 1
    assert status(client, day)["needs_refill"] is False
    at(23, 19, 5)
    take(client, day, reader, "evening", ["r0c0", "r0c1", "r0c2"])

    # 저녁 뒤: 오늘 채워야 함
    st = status(client, day)
    assert st == {"remaining_cells": 0, "covered_until": None, "needs_refill": True,
                  "threshold_cells": 0}  # fmt: skip
    s = summary(client, day, "2026-10-23")
    assert s["all_normal"] is True and s["counts"]["taken_ai"] == 3
    assert s["refill"] == {"needs_refill": True, "remaining_cells": 0, "covered_until": None}
    assert summary_body(s, type("F", (), {"elder_display_name": "어머니"})) == (
        "어머니 오늘 3번 모두 정상이에요 · 약통을 채워 주세요 (모의 판독 포함)"
    )
    assert open_handoffs(client, day) == []

    at(23, 20, 0)
    refill(client, day, reader)
    st = status(client, day)
    assert st["remaining_cells"] == 3 and st["needs_refill"] is False
    assert covered(st) == ("2026-10-24", "evening")
    assert summary(client, day, "2026-10-23")["refill"]["needs_refill"] is False

    # 다음 날 아침 정상
    at(24, 8, 5)
    res = take(client, day, reader, "morning", ["r0c0"]).json()
    assert res["dose_event"]["outcome"] == "taken"
    assert res["dose_event"]["latest_reading"] is None  # 어르신 형태
    assert status(client, day)["remaining_cells"] == 2
    assert open_handoffs(client, day) == []
    assert [m.data.get("reason") for m in push.sent if m.data.get("type") == "handoff"] == []
    week = client.get(
        "/api/v1/summaries?period=week&from=2026-10-19&to=2026-10-25", headers=day.g
    ).json()["items"][0]
    assert week["counts"]["taken_ai"] == 4 and week["refill"]["remaining_cells"] == 2


def test_daily_without_refill_goes_refill_overdue_every_dose(client, day, reader, push):
    use_friday(client, day, reader)
    # 채우지 않고 토요일 아침: 약통이 비어 있음 → 정상 기록 금지, refill_overdue
    at(24, 8, 5)
    reader.states = {"r0c0": "empty", "r0c1": "empty", "r0c2": "empty"}
    res = upload(client, day, event_id(client, day, "morning")).json()
    assert res["result"] == "guardian_notified" and res["dose_event"]["outcome"] is None
    hs = open_handoffs(client, day)
    assert [h["reason"] for h in hs] == ["refill_overdue"]
    res = client.post(
        f"/api/v1/handoffs/{hs[0]['id']}/verdict", json={"verdict": "acknowledged"}, headers=day.g
    )
    assert res.status_code == 200, res.text
    st = status(client, day)
    assert st["remaining_cells"] == 0 and st["needs_refill"] is True

    # 일요일 아침에도 아직 안 채움: 인계로 끝난 칸도 채우기 전까지 빈 칸으로 본다
    at(25, 8, 5)
    res = upload(client, day, event_id(client, day, "morning")).json()
    assert res["result"] == "guardian_notified" and res["dose_event"]["outcome"] is None
    # (토요일 점심·저녁은 사진이 없어 무응답 인계)
    sun = [
        h for h in open_handoffs(client, day) if h["dose_event"]["id"] == res["dose_event"]["id"]
    ]
    assert [h["reason"] for h in sun] == ["refill_overdue"]

    # 채우면 다시 정상
    at(25, 9, 0)
    refill(client, day, reader)
    at(25, 12, 5)
    res = take(client, day, reader, "noon", ["r0c1"]).json()
    assert res["dose_event"]["outcome"] == "taken"


def test_daily_refilled_but_not_registered_adopts_baseline(client, day, reader):
    use_friday(client, day, reader)
    # 채웠지만 기록하지 않음: 아침 칸만 비고 나머지 차 있음 → 묻지 않고 그 사진을 새 기준선으로 (2026-11-14)
    at(24, 8, 5)
    reader.states = {"r0c0": "empty"}
    res = upload(client, day, event_id(client, day, "morning")).json()
    assert res["result"] == "recorded"
    assert open_handoffs(client, day) == []
    st = status(client, day)
    assert st["remaining_cells"] == 2 and covered(st) == ("2026-10-24", "evening")
    at(24, 12, 5)
    assert take(client, day, reader, "noon", ["r0c0", "r0c1"]).json()["result"] == "recorded"


def test_two_day_pillbox_threshold(client):
    """6칸·하루 3번 = 이틀 약통: 기준은 하루치(3칸)."""
    f = Family(client)
    f.register_pillbox(rows=2, cols=3)
    slots = [
        {"weekday": wd, "time_of_day": tod, "dose_time": t, "cell_id": f"r{wd % 2}c{i}"}
        for wd in range(7)
        for i, (tod, t, _) in enumerate(DAILY)
    ]
    f.put_schedule(slots)
    st = status(client, f)
    assert st["threshold_cells"] == 3 and st["remaining_cells"] == 6
    assert st["needs_refill"] is False
