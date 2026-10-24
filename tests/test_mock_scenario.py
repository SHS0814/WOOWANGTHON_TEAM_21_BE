"""QA용 결정적 모의 판독 POST /dev/mock-scenario (R-018)."""

import pytest

from tests.conftest import Family, week_schedule
from tests.test_dose import at, event_id, open_handoffs, today, upload


@pytest.fixture
def demo(client, settings, reader):
    settings.demo_mode = True
    reader.provider = "mock"  # 매일 판독기가 mock 인 서버
    f = Family(client, is_demo=True)
    f.register_pillbox()
    f.put_schedule(week_schedule())
    return f


def scenario(client, f, name):
    return client.post("/api/v1/dev/mock-scenario", json={"scenario": name}, headers=f.e)


@pytest.mark.parametrize(
    ("name", "result", "reason"),
    [
        ("normal", "recorded", None),
        ("unclear", "retake", None),
        ("unchanged", "remind_retake", None),
        ("double_suspected", "guardian_notified", "double_suspected"),
    ],
)
def test_each_scenario_drives_real_flow(client, demo, reader, name, result, reason):
    reader.default = "unclear"  # 시나리오가 없으면 나올 결과와 다르게 둔다
    at(23, 8, 5)
    res = scenario(client, demo, name)
    assert res.status_code == 200
    assert res.json() == {"scenario": name, "applies_to": "next_dose_photo", "is_mock": True}
    body = upload(client, demo, event_id(client, demo, "morning")).json()
    assert body["result"] == result and body["is_mock"] is True
    hs = open_handoffs(client, demo)
    assert [h["reason"] for h in hs] == ([reason] if reason else [])
    g = today(client, demo.g)["items"][0]
    assert g["is_mock"] is True
    assert g["latest_reading"]["provider"] == "mock"


def test_scenario_is_one_shot_and_clearable(client, demo, reader):
    at(23, 8, 5)
    eid = event_id(client, demo, "morning")
    scenario(client, demo, "unclear")
    assert upload(client, demo, eid).json()["result"] == "retake"
    reader.states = {"r0c4": "empty"}  # 시나리오가 소비된 뒤에는 원래 판독기
    assert upload(client, demo, eid).json()["result"] == "recorded"
    scenario(client, demo, "unchanged")
    assert scenario(client, demo, None).json()["scenario"] is None
    at(23, 19, 5)
    reader.states = {"r0c4": "empty", "r1c4": "empty"}
    assert upload(client, demo, event_id(client, demo, "evening")).json()["result"] == "recorded"


def test_scenario_requires_demo_mode_demo_family_and_mock_reader(client, settings, reader):
    reader.provider = "mock"
    settings.demo_mode = True
    normal = Family(client)
    assert scenario(client, normal, "normal").status_code == 404
    demo = Family(client, is_demo=True)
    assert scenario(client, demo, "normal").status_code == 200
    reader.provider = "claude"
    assert scenario(client, demo, "normal").status_code == 404
    reader.provider = "mock"
    settings.demo_mode = False
    assert scenario(client, demo, "normal").status_code == 404
    settings.demo_mode = True
    res = client.post("/api/v1/dev/mock-scenario", json={"scenario": "weird"}, headers=demo.e)
    assert res.status_code == 422
