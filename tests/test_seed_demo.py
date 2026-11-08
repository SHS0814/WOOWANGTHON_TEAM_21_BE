from app.seed_demo import main, revoke_demo_devices, seed
from tests.conftest import bearer


def test_seed_creates_ready_demo_family(client, settings, reader):
    settings.demo_mode = True
    reader.default = "empty"  # 판독기가 뭐라고 하든 기준선은 모두 채움으로 맞춘다
    out = seed(client)
    g, e = bearer(out["guardian_token"]), bearer(out["elder_token"])
    me = client.get("/api/v1/me", headers=e).json()
    assert me["family"]["is_demo"] is True and me["consent_required"] is False
    assert me["family"]["has_pillbox"] and me["family"]["has_schedule"]
    cur = client.get("/api/v1/pillboxes/current", headers=g).json()
    assert cur["pillbox"]["layout"]["rows"] == 2 and cur["pillbox"]["layout"]["cols"] == 7
    assert {c["state"] for c in cur["last_refill"]["baseline_cells"]} == {"full"}
    assert cur["last_refill"]["needs_review"] is False
    assert cur["refill_status"]["remaining_cells"] == 14
    assert len(client.get("/api/v1/schedule", headers=g).json()["slots"]) == 14
    assert client.get("/api/v1/settings", headers=g).json()["elder_refill_prompt"] is False
    # 데모 가족이라 시연 트리거가 열린다
    slots = client.get("/api/v1/schedule", headers=g).json()["slots"]
    fri_pm = next(s for s in slots if s["weekday"] == 4 and s["time_of_day"] == "evening")
    assert (
        client.post(
            "/api/v1/dev/dose-events", json={"slot_id": fri_pm["id"]}, headers=e
        ).status_code
        == 201
    )


def test_seed_needs_demo_mode(client):
    import pytest

    with pytest.raises(SystemExit):
        seed(client)


def test_revoke_demo_devices(tmp_path, settings, push, now, reader):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import Family

    settings.database_url = f"sqlite:///{tmp_path / 'db.sqlite3'}"
    settings.demo_mode = True
    settings.jobs_enabled = False
    client = TestClient(create_app(settings, push=push, cells_reader=reader))
    out = seed(client)
    normal = Family(client)  # 데모가 아닌 가족은 건드리지 않는다
    assert revoke_demo_devices(settings.database_url) == 2
    res = client.get("/api/v1/me", headers=bearer(out["guardian_token"]))
    assert res.json()["error"]["code"] == "token_revoked"
    assert client.get("/api/v1/me", headers=normal.g).status_code == 200


def test_cli_requires_subcommand(capsys):
    import pytest

    with pytest.raises(SystemExit):
        main([])


def test_seed_daily_pillbox(client, settings, reader):
    settings.demo_mode = True
    out = seed(client, pillbox="day")
    g = bearer(out["guardian_token"])
    cur = client.get("/api/v1/pillboxes/current", headers=g).json()
    assert cur["pillbox"]["layout"]["rows"] == 1 and cur["pillbox"]["layout"]["cols"] == 3
    assert cur["refill_status"]["remaining_cells"] == 3
    assert cur["refill_status"]["threshold_cells"] == 0
    assert cur["refill_status"]["needs_refill"] is False
    assert client.get("/api/v1/settings", headers=g).json()["elder_refill_prompt"] is True
    slots = client.get("/api/v1/schedule", headers=g).json()["slots"]
    assert len(slots) == 21
    assert {(s["time_of_day"], s["cell_id"]) for s in slots} == {
        ("morning", "r0c0"),
        ("noon", "r0c1"),
        ("evening", "r0c2"),
    }
