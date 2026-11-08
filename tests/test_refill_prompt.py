"""마지막 칸을 쓴 정상 기록 뒤 어르신 "약통을 채워 주세요" (R-071, api.md 3.18·3.26).

시각: T0 = 2026-10-23(금) 07:00 KST.
"""

from sqlalchemy import create_engine, text

from app.db import ensure_schema
from tests.test_daily_pillbox import day, refill  # noqa: F401 (day: fixture)
from tests.test_dose import at, event_id, fam, take, upload  # noqa: F401 (fam: fixture)


def prompt_on(client, f, on: bool = True) -> None:
    res = client.put("/api/v1/settings", json={"elder_refill_prompt": on}, headers=f.g)
    assert res.status_code == 200 and res.json()["elder_refill_prompt"] is on


def test_settings_field(client, day):  # noqa: F811
    assert client.get("/api/v1/settings", headers=day.g).json()["elder_refill_prompt"] is False
    prompt_on(client, day)
    assert client.get("/api/v1/settings", headers=day.g).json()["elder_refill_prompt"] is True
    # 다른 필드만 바꾸면 그대로
    res = client.put("/api/v1/settings", json={"summary_push_enabled": True}, headers=day.g)
    assert res.json()["elder_refill_prompt"] is True
    prompt_on(client, day, False)
    res = client.put("/api/v1/settings", json={"elder_refill_prompt": "maybe"}, headers=day.g)
    assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error"
    assert client.get("/api/v1/settings", headers=day.e).status_code == 403


def test_daily_pillbox_only_after_evening(client, day, reader):  # noqa: F811
    prompt_on(client, day)
    at(23, 8, 5)
    assert take(client, day, reader, "morning", ["r0c0"]).json()["refill_prompt"] is False
    at(23, 12, 5)
    assert take(client, day, reader, "noon", ["r0c0", "r0c1"]).json()["refill_prompt"] is False
    at(23, 19, 5)
    eid = event_id(client, day, "evening")
    reader.states = {"r0c0": "empty", "r0c1": "empty", "r0c2": "empty"}
    first = upload(client, day, eid, client_upload_id="pm-1").json()
    assert first["result"] == "recorded" and first["refill_prompt"] is True
    # 같은 client_upload_id 재전송: 처음과 같은 값 (설정을 꺼도)
    prompt_on(client, day, False)
    again = upload(client, day, eid, client_upload_id="pm-1").json()
    assert again["result"] == "recorded" and again["refill_prompt"] is True
    # 채운 뒤 다음 날 아침은 다시 false
    prompt_on(client, day)
    at(23, 20, 0)
    refill(client, day, reader)
    at(24, 8, 5)
    assert take(client, day, reader, "morning", ["r0c0"]).json()["refill_prompt"] is False


def test_daily_evening_off_by_default(client, day, reader):  # noqa: F811
    at(23, 8, 5)
    take(client, day, reader, "morning", ["r0c0"])
    at(23, 12, 5)
    take(client, day, reader, "noon", ["r0c0", "r0c1"])
    at(23, 19, 5)
    res = take(client, day, reader, "evening", ["r0c0", "r0c1", "r0c2"]).json()
    assert res["refill_prompt"] is False


def test_weekly_last_cell_follows_setting(client, fam, reader):  # noqa: F811
    # 금요일 07:00 기준선: 금 아침 칸(r0c4)만 남기고 다 비었다고 고친다
    cur = client.get("/api/v1/pillboxes/current", headers=fam.g).json()
    cells = [
        {"cell_id": c["cell_id"], "state": "full" if c["cell_id"] == "r0c4" else "empty"}
        for c in cur["last_refill"]["baseline_cells"]
    ]
    res = client.put(
        f"/api/v1/refills/{cur['last_refill']['id']}/baseline", json={"cells": cells}, headers=fam.g
    )
    assert res.status_code == 200
    assert (
        client.get("/api/v1/pillboxes/current", headers=fam.g).json()["refill_status"][
            "remaining_cells"
        ]
        == 1
    )
    prompt_on(client, fam)
    at(23, 8, 5)
    empty = [c["cell_id"] for c in cells]  # 사진에서도 모두 비어 보임
    res = take(client, fam, reader, "morning", empty).json()
    assert res["result"] == "recorded" and res["refill_prompt"] is True


def test_weekly_last_cell_setting_off(client, fam, reader):  # noqa: F811
    cur = client.get("/api/v1/pillboxes/current", headers=fam.g).json()
    cells = [
        {"cell_id": c["cell_id"], "state": "full" if c["cell_id"] == "r0c4" else "empty"}
        for c in cur["last_refill"]["baseline_cells"]
    ]
    client.put(
        f"/api/v1/refills/{cur['last_refill']['id']}/baseline", json={"cells": cells}, headers=fam.g
    )
    at(23, 8, 5)
    res = take(client, fam, reader, "morning", [c["cell_id"] for c in cells]).json()
    assert res["result"] == "recorded" and res["refill_prompt"] is False


def test_weekly_not_last_cell(client, fam, reader):  # noqa: F811
    prompt_on(client, fam)
    at(23, 8, 5)
    assert take(client, fam, reader, "morning", ["r0c4"]).json()["refill_prompt"] is False


def test_not_recorded_results_are_false(client, day, reader):  # noqa: F811
    prompt_on(client, day)
    at(23, 8, 5)
    take(client, day, reader, "morning", ["r0c0"])
    at(23, 12, 5)
    take(client, day, reader, "noon", ["r0c0", "r0c1"])
    at(23, 19, 5)
    eid = event_id(client, day, "evening")
    # 애매 → retake
    reader.states = {"r0c0": "empty", "r0c1": "empty", "r0c2": "unclear"}
    res = upload(client, day, eid).json()
    assert res["result"] == "retake" and res["refill_prompt"] is False
    # 두 번째 애매 → guardian_notified
    res = upload(client, day, eid).json()
    assert res["result"] == "guardian_notified" and res["refill_prompt"] is False
    # 칸 그대로 → remind_retake (다른 건으로)
    at(24, 8, 5)
    reader.states = {}
    eid = event_id(client, day, "morning")
    res = upload(client, day, eid).json()
    assert res["result"] in ("remind_retake", "guardian_notified")
    assert res["refill_prompt"] is False


def test_existing_db_gets_column_default_false(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.sqlite3'}"
    engine = create_engine(url)
    ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE families DROP COLUMN elder_refill_prompt"))
        conn.execute(text("ALTER TABLE photos DROP COLUMN upload_refill_prompt"))
        conn.execute(
            text(
                "INSERT INTO families (id, elder_display_name, summary_push_enabled,"
                " summary_push_time, photo_retention_days, unchanged_mode, is_demo, created_at)"
                " VALUES ('f1', '어머니', 0, '21:00', 90, 'remind_then_retake', 0,"
                " '2026-10-23 00:00:00')"
            )
        )
    ensure_schema(engine)  # 서버 시작 때와 같이 빠진 열을 붙인다
    with engine.connect() as conn:
        assert conn.execute(text("SELECT elder_refill_prompt FROM families")).scalar() is None
        conn.execute(text("SELECT upload_refill_prompt FROM photos"))
