from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from app.clock import clock
from tests.conftest import SAMPLE_PNG, Family, week_schedule


def _files(data=SAMPLE_PNG, name="box.png", mime="image/png"):
    return {"photo": (name, data, mime)}


def _path(url: str) -> str:
    u = urlparse(url)
    return f"{u.path}?{u.query}"


def test_detect_returns_mock_proposal_and_signed_photo(client):
    f = Family(client)
    res = client.post("/api/v1/pillboxes/detect", files=_files(), headers=f.g)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_mock"] is True and body["provider"] == "mock"
    assert (
        body["proposal"]["layout"]["rows"] == 2 and len(body["proposal"]["layout"]["cells"]) == 14
    )
    assert body["proposal"]["suggested_time_of_day"] == ["morning", "evening"]
    url = body["photo"]["url"]
    assert url.startswith("https://test.example/api/v1/photo-files/")
    # 서명 URL 은 토큰 없이 열린다
    img = client.get(_path(url))
    assert img.status_code == 200 and img.content == SAMPLE_PNG
    assert img.headers["content-type"] == "image/png"
    # 서명 변조, 만료
    q = parse_qs(urlparse(url).query)
    bad = client.get(f"/api/v1/photo-files/{body['photo']['id']}?exp={q['exp'][0]}&sig=00")
    assert bad.json()["error"]["code"] == "signature_invalid"
    clock.set(clock.now() + timedelta(minutes=6))
    assert client.get(_path(url)).json()["error"]["code"] == "signature_expired"
    fresh = client.get(f"/api/v1/photos/{body['photo']['id']}", headers=f.e).json()
    assert client.get(_path(fresh["url"])).status_code == 200


def test_upload_validation(client):
    f = Family(client)
    res = client.post(
        "/api/v1/pillboxes/detect", files=_files(b"hello", "a.txt", "text/plain"), headers=f.g
    )
    assert res.status_code == 415 and res.json()["error"]["code"] == "unsupported_media"
    res = client.post("/api/v1/pillboxes/detect", files=_files(), headers=f.e)
    assert res.status_code == 403


def test_register_pillbox_creates_baseline(client):
    f = Family(client)
    body = f.register_pillbox()
    assert body["pillbox"]["active"] and len(body["pillbox"]["layout"]["cells"]) == 14
    refill = body["refill"]
    assert refill["source"] == "registration"
    assert refill["filled_by"]["display_name"] == "민수"
    reading = refill["baseline_reading"]
    assert reading["is_mock"] is True and reading["classification"] is None
    assert all("confidence" not in c for c in reading["cells"])
    assert all(c["expected"] is None for c in reading["cells"])
    by_cell = {c["cell_id"]: c for c in reading["cells"]}
    for c in refill["baseline_cells"]:
        ai = by_cell[c["cell_id"]]["ai_state"]
        assert c["state"] == ("empty" if ai == "empty" else "full")
    unclear = [c for c in reading["cells"] if c["ai_state"] == "unclear"]
    assert refill["needs_review"] == bool(unclear)
    assert all(c["attention"] == "unclear" for c in unclear)
    me = client.get("/api/v1/me", headers=f.g).json()
    assert me["family"]["has_pillbox"] is True


def test_register_pillbox_validation(client):
    f = Family(client)
    other = Family(client)
    det = client.post("/api/v1/pillboxes/detect", files=_files(), headers=other.g).json()
    layout = {"rows": 2, "cols": 7}
    res = client.post(
        "/api/v1/pillboxes", json={"photo_id": det["photo"]["id"], "layout": layout}, headers=f.g
    )
    assert res.status_code == 404
    det = client.post("/api/v1/pillboxes/detect", files=_files(), headers=f.g).json()
    res = client.post(
        "/api/v1/pillboxes",
        json={"photo_id": det["photo"]["id"], "layout": {"rows": 9, "cols": 7}},
        headers=f.g,
    )
    assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error"
    res = client.post(
        "/api/v1/pillboxes",
        json={
            "photo_id": det["photo"]["id"],
            "layout": {"rows": 2, "cols": 7, "row_labels": ["a"]},
        },
        headers=f.g,
    )
    assert res.status_code == 422


def test_pillbox_required(client):
    f = Family(client)
    res = client.get("/api/v1/pillboxes/current", headers=f.g)
    assert res.status_code == 404 and res.json()["error"]["code"] == "pillbox_required"
    res = client.put("/api/v1/schedule", json={"slots": []}, headers=f.g)
    assert res.status_code == 409 and res.json()["error"]["code"] == "pillbox_required"
    res = client.post("/api/v1/refills", files=_files(), headers=f.g)
    assert res.status_code == 409 and res.json()["error"]["code"] == "pillbox_required"


def test_schedule_put_get_and_validation(client):
    f = Family(client)
    f.register_pillbox()
    slots = week_schedule()
    out = f.put_schedule(slots)
    assert len(out["slots"]) == 14 and out["updated_at"].endswith("+09:00")
    # 어르신도 일정을 읽는다 (오프라인 알림용)
    assert len(client.get("/api/v1/schedule", headers=f.e).json()["slots"]) == 14
    # 같은 요일에 같은 칸 두 번 (slots[7] = 월 저녁)
    bad = [slots[0], {**slots[7], "cell_id": slots[0]["cell_id"]}]
    res = client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g)
    assert res.status_code == 422
    assert res.json()["error"]["details"]["fields"][0]["field"] == "slots.1.cell_id"
    bad = [{**slots[0], "cell_id": "r9c9"}]
    assert client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g).status_code == 422
    bad = [slots[0], {**slots[7], "time_of_day": "morning", "weekday": 0}]
    assert client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g).status_code == 422
    bad = [{**slots[0], "dose_time": "8:00"}]
    assert client.put("/api/v1/schedule", json={"slots": bad}, headers=f.g).status_code == 422


def test_refill_status_threshold_and_coverage(client):
    f = Family(client)
    reg = f.register_pillbox()
    f.put_schedule(week_schedule())
    refill_id = reg["refill"]["id"]
    all_full = [{"cell_id": c["cell_id"], "state": "full"} for c in reg["refill"]["baseline_cells"]]
    res = client.put(f"/api/v1/refills/{refill_id}/baseline", json={"cells": all_full}, headers=f.g)
    assert res.status_code == 200 and res.json()["needs_review"] is False
    cur = client.get("/api/v1/pillboxes/current", headers=f.g).json()
    st = cur["refill_status"]
    # 하루 2회 × 2일 = 4칸 (R-010 5번)
    assert st["threshold_cells"] == 4
    assert st["remaining_cells"] == 14 and st["needs_refill"] is False
    # 금요일 07:00 기준, 다음 주 목요일 저녁까지
    assert st["covered_until"] == {
        "date": "2026-10-29",
        "weekday": 3,
        "time_of_day": "evening",
        "dose_time": "19:00",
    }
    assert cur["pillbox_mismatch"] == {
        "detected": False,
        "cell_ids": [],
        "reading_id": None,
        "detected_at": None,
    }
    # 토요일 아침 칸이 비어 있다고 고치면 금요일 저녁까지
    res = client.put(
        f"/api/v1/refills/{refill_id}/baseline",
        json={
            "cells": [{"cell_id": "r0c5", "state": "empty"}, {"cell_id": "r1c0", "state": "empty"}]
        },
        headers=f.g,
    )
    st = client.get("/api/v1/pillboxes/current", headers=f.g).json()["refill_status"]
    assert st["remaining_cells"] == 12
    assert (
        st["covered_until"]["date"] == "2026-10-23"
        and st["covered_until"]["time_of_day"] == "evening"
    )


def test_refill_with_photo_and_latest_only(client):
    f = Family(client)
    reg = f.register_pillbox()
    clock.set(clock.now() + timedelta(days=6))
    res = client.post("/api/v1/refills", files=_files(), headers=f.g)
    assert res.status_code == 201
    refill = res.json()
    assert refill["source"] == "guardian_photo" and refill["baseline_reading"]["is_mock"] is True
    cur = client.get("/api/v1/pillboxes/current", headers=f.g).json()
    assert cur["last_refill"]["id"] == refill["id"]
    res = client.put(
        f"/api/v1/refills/{reg['refill']['id']}/baseline",
        json={"cells": [{"cell_id": "r0c0", "state": "full"}]},
        headers=f.g,
    )
    assert res.status_code == 409 and res.json()["error"]["code"] == "refill_not_latest"
    res = client.put(
        f"/api/v1/refills/{refill['id']}/baseline",
        json={"cells": [{"cell_id": "zz", "state": "full"}]},
        headers=f.g,
    )
    assert res.status_code == 422


def test_reregister_pillbox_retires_schedule(client):
    f = Family(client)
    f.register_pillbox()
    f.put_schedule(week_schedule())
    f.register_pillbox(rows=1, cols=7)
    assert client.get("/api/v1/schedule", headers=f.g).json()["slots"] == []
    cur = client.get("/api/v1/pillboxes/current", headers=f.g).json()
    assert cur["pillbox"]["layout"]["rows"] == 1
    assert cur["refill_status"]["threshold_cells"] == 0


def test_daily_reading_limit(client, settings):
    settings.daily_reading_limit = 2
    f = Family(client)
    for _ in range(2):
        assert (
            client.post("/api/v1/pillboxes/detect", files=_files(), headers=f.g).status_code == 200
        )
    res = client.post("/api/v1/pillboxes/detect", files=_files(), headers=f.g)
    assert res.status_code == 429 and res.json()["error"]["code"] == "daily_reading_limit"


def test_baseline_reads_use_baseline_purpose(client, reader):
    f = Family(client)
    f.register_pillbox()
    client.post("/api/v1/refills", files=_files(), headers=f.g)
    assert reader.purposes == ["baseline", "baseline"]
