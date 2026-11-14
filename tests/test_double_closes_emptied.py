"""이중 복용 의심을 보호자가 "중복 복용"으로 판정하면, 미리 비워진 칸의 같은 날 남은 복약 건을 닫는다.

닫지 않으면 그 칸의 복약 시각에 어르신이 빈 칸을 찍고, 판독이 "정상, 드심"으로 기록해 버린다.
"""

import pytest

from tests.test_dose import at, event_id, fam, jobs, open_handoffs, today, upload  # noqa: F401


def _double_at_morning(client, fam, reader):  # noqa: F811
    """2026-10-23(금) 아침에 아침 칸(r0c4)과 저녁 칸(r1c4)이 같이 비어 보인다."""
    at(23, 8, 5)
    reader.states = {"r0c4": "empty", "r1c4": "empty"}
    res = upload(client, fam, event_id(client, fam, "morning"))
    assert res.json()["result"] == "guardian_notified"
    (h,) = open_handoffs(client, fam)
    assert h["reason"] == "double_suspected"
    return h["id"]


def _evening(client, fam):  # noqa: F811
    items = today(client, fam.g)["items"]
    return next(e for e in items if e["slot"]["time_of_day"] == "evening")


def test_double_verdict_closes_pre_emptied_cell_event(client, fam, reader, push):  # noqa: F811
    hid = _double_at_morning(client, fam, reader)
    assert _evening(client, fam)["state"] == "scheduled"

    res = client.post(
        f"/api/v1/handoffs/{hid}/verdict", json={"verdict": "acknowledged"}, headers=fam.g
    )
    assert res.status_code == 200, res.text

    ev = _evening(client, fam)
    assert ev["state"] == "done"
    assert ev["outcome"] == "acknowledged"  # "정상 복용"으로 적지 않는다
    assert ev["outcome_source"] == "guardian"
    assert ev["handoff"] is None  # 화면은 인계 없는 acknowledged 를 "건너뜀"으로 보여 준다

    # 어르신의 "나 약 먹었나?"에는 아침·저녁이 같은 사진, 같은 시각으로 "기록 있음"으로 나온다
    elder = {e["slot"]["time_of_day"]: e for e in today(client, fam.e)["items"]}
    morning, evening = elder["morning"], elder["evening"]
    assert morning["elder_status"] == evening["elder_status"] == "recorded"
    assert morning["taken_at"] == evening["taken_at"] is not None
    assert [p["id"] for p in evening["photos"]] == [morning["photos"][-1]["id"]]

    # 저녁 시각이 지나도 어르신께 묻지 않고, 인계도 생기지 않는다
    at(23, 19, 45)
    jobs(client)
    assert _evening(client, fam)["state"] == "done"
    assert open_handoffs(client, fam) == []
    elder = next(e for e in today(client, fam.e)["items"] if e["slot"]["time_of_day"] == "evening")
    assert elder["state"] == "done"


@pytest.mark.parametrize("verdict", ["taken", "not_taken"])
def test_other_verdicts_keep_evening_open(client, fam, reader, verdict):  # noqa: F811
    """정상 복용(AI가 잘못 봄)·미복용으로 판정하면 저녁은 그대로 묻는다."""
    hid = _double_at_morning(client, fam, reader)
    res = client.post(f"/api/v1/handoffs/{hid}/verdict", json={"verdict": verdict}, headers=fam.g)
    assert res.status_code == 200, res.text
    assert _evening(client, fam)["state"] == "scheduled"


def test_before_verdict_evening_is_still_asked(client, fam, reader):  # noqa: F811
    _double_at_morning(client, fam, reader)
    at(23, 19, 5)
    jobs(client)
    assert _evening(client, fam)["state"] == "asked"


def test_other_days_cell_is_not_closed(client, fam, reader):  # noqa: F811
    """내일 칸(r0c5)이 미리 비어 있어도 오늘 건만 본다. 오늘 저녁 건은 그대로 열린다."""
    at(23, 8, 5)
    reader.states = {"r0c4": "empty", "r0c5": "empty"}
    upload(client, fam, event_id(client, fam, "morning"))
    (h,) = open_handoffs(client, fam)
    client.post(
        f"/api/v1/handoffs/{h['id']}/verdict", json={"verdict": "acknowledged"}, headers=fam.g
    )
    assert _evening(client, fam)["state"] == "scheduled"


def test_closing_wakes_elder_phone(client, fam, reader, push):  # noqa: F811
    client.put("/api/v1/devices/me/push-token", json={"push_token": "elder-tok"}, headers=fam.e)
    hid = _double_at_morning(client, fam, reader)
    push.sent.clear()
    client.post(f"/api/v1/handoffs/{hid}/verdict", json={"verdict": "acknowledged"}, headers=fam.g)
    silent = [m for m in push.sent if m.silent]
    assert [m.push_token for m in silent] == ["elder-tok"]
