import json
from datetime import datetime, timedelta

import httpx

from app.clock import clock
from app.jobs import run_once
from app.push import ExpoPushSender, PushMessage
from app.timeutil import KST
from tests.test_dose import at, fam, open_handoffs, take, today  # noqa: F401 (fam: fixture)


def _day(client, f, date: str) -> dict:
    run_once(client.app)
    res = client.get(f"/api/v1/summaries?period=day&from={date}&to={date}", headers=f.g)
    assert res.status_code == 200, res.text
    return res.json()["items"][0]


def test_day_summary_all_normal(client, fam, reader):  # noqa: F811
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    at(23, 19, 5)
    take(client, fam, reader, "evening", ["r0c4", "r1c4"])
    s = _day(client, fam, "2026-10-23")
    assert s["all_normal"] is True and s["final"] is False
    assert s["counts"] == {
        "scheduled": 2,
        "taken_ai": 2,
        "taken_guardian": 0,
        "not_taken": 0,
        "acknowledged": 0,
        "open_handoffs": 0,
        "pending": 0,
    }
    assert s["includes_mock"] is True and s["includes_simulated"] is False
    assert s["pillbox_mismatch"] is False
    assert s["refill"]["remaining_cells"] == 12
    assert set(s["handoffs_by_reason"]) == {
        "no_response",
        "unclear",
        "unchanged",
        "double_suspected",
        "refill_unconfirmed",
        "refill_overdue",
    }


def test_day_summary_with_handoff_and_defaults(client, fam, reader):  # noqa: F811
    at(23, 8, 31)
    s = _day(client, fam, "2026-10-23")
    assert s["all_normal"] is False
    assert s["counts"]["open_handoffs"] == 1 and s["handoffs_by_reason"]["no_response"] == 1
    items = client.get("/api/v1/summaries?period=day", headers=fam.g).json()["items"]
    assert [i["period_start"] for i in items][-1] == "2026-10-23" and len(items) == 7
    weeks = client.get("/api/v1/summaries?period=week", headers=fam.g).json()["items"]
    assert weeks[-1]["period_start"] == "2026-10-19" and weeks[-1]["period_end"] == "2026-10-25"
    # 하루 요약 최대 366일 (R-051, 이전 31일)
    res = client.get("/api/v1/summaries?period=day&from=2025-10-22&to=2026-10-23", headers=fam.g)
    assert res.status_code == 422
    assert client.get("/api/v1/summaries?period=day", headers=fam.e).status_code == 403


def test_jobs_hand_off_no_response_without_requests(client, fam, push):  # noqa: F811
    today(client, fam.g)  # 오늘 복약 건 생성
    run_once(client.app, datetime(2026, 10, 23, 8, 31, tzinfo=KST))
    assert [m.data.get("reason") for m in push.sent] == ["no_response"]
    clock.set(datetime(2026, 10, 23, 8, 32, tzinfo=KST))
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["no_response"]


def test_summary_push_only_when_enabled(client, fam, reader, push):  # noqa: F811
    at(23, 8, 5)
    take(client, fam, reader, "morning", ["r0c4"])
    run_once(client.app, datetime(2026, 10, 23, 21, 0, tzinfo=KST))
    assert [m for m in push.sent if m.data.get("type") == "summary"] == []
    client.put("/api/v1/settings", json={"summary_push_enabled": True}, headers=fam.g)
    t = datetime(2026, 10, 23, 21, 0, tzinfo=KST)
    run_once(client.app, t)
    run_once(client.app, t)  # 같은 분에 두 번 돌아도 1건
    summaries = [m for m in push.sent if m.data.get("type") == "summary"]
    assert len(summaries) == 1
    assert summaries[0].body.startswith("어머니 오늘 2번 중 1번 기록")
    assert "모의 판독 포함" in summaries[0].body


def test_cleanup_deletes_old_photos(client, fam, reader, settings):  # noqa: F811
    at(23, 8, 5)
    res = take(client, fam, reader, "morning", ["r0c4"]).json()
    photo = res["dose_event"]["photos"][0]
    client.put("/api/v1/settings", json={"photo_retention_days": 1}, headers=fam.g)
    later = datetime(2026, 10, 25, 3, 0, tzinfo=KST)
    run_once(client.app, later)
    clock.set(later + timedelta(minutes=1))
    res = client.get(f"/api/v1/photos/{photo['id']}", headers=fam.g)
    assert res.status_code == 410 and res.json()["error"]["code"] == "photo_expired"


def test_expo_sender_batches_and_skips_missing_tokens():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.headers.get("authorization"), body))
        return httpx.Response(200, json={"data": [{"status": "ok"} for _ in body]})

    sender = ExpoPushSender("tok", transport=httpx.MockTransport(handler))
    msgs = [PushMessage("d1", "ExponentPushToken[a]", "t", "b", {"type": "x"})]
    msgs.append(PushMessage("d2", None, "t", "b", {}))
    sender.send(msgs)
    assert len(seen) == 1 and seen[0][0] == "Bearer tok"
    assert seen[0][1] == [
        {
            "to": "ExponentPushToken[a]",
            "title": "t",
            "body": "b",
            "data": {"type": "x"},
            "sound": "default",
            "priority": "high",
        }
    ]
    assert [m.device_id for m in sender.sent] == ["d1"]


def test_expo_sender_failure_does_not_raise():
    sender = ExpoPushSender(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    sender.send([PushMessage("d1", "ExponentPushToken[a]", "t", "b", {})])
    assert sender.sent == []


def test_loop_timing_detects_late_runs_and_sleep():
    """R-021 Q-008: 실제 간격과 지연을 재고, PC 잠자기를 구분한다."""
    from app.jobs import LoopTiming

    t = LoopTiming(60, summary_every=3)
    assert t.started(1000.0, 500.0) is None  # 첫 회차
    assert t.finished(0.2) is None
    assert t.started(1060.0, 560.0) is None  # 정상 간격
    assert t.finished(0.3) is None
    # 벽시계로 986초 지났지만 단조 시계는 60초: 그동안 PC 가 잠들어 있었다
    warn = t.started(2046.0, 620.0)
    assert "interval=986s" in warn and "asleep ~926s" in warn
    note = t.finished(0.1)
    assert note.startswith("jobs ok: 3 runs") and "max interval 986s" in note
    # 잠들지 않았는데 늦으면(서버가 바빴음) 잠자기 문구 없음
    warn = t.started(2200.0, 774.0)
    assert "interval=154s" in warn and "asleep" not in warn
    assert t.finished(45.0).startswith("jobs slow")
