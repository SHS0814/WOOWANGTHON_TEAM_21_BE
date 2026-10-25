"""어르신 폰 알림 준비 상태 보고 (R-032, api.md 3.29)."""

from sqlalchemy import create_engine, inspect, text

from app.db import ensure_schema
from tests.conftest import Family
from tests.test_dose import at

PATH = "/api/v1/devices/me/notification-status"


def _report(client, headers, allowed, exact):
    return client.put(
        PATH, json={"notifications_allowed": allowed, "exact_alarm_allowed": exact}, headers=headers
    )


def _warning(client, headers):
    return client.get("/api/v1/dose-events/today", headers=headers).json()[
        "elder_notification_warning"
    ]


def _elder_device(client, f):
    items = client.get("/api/v1/devices", headers=f.g).json()["items"]
    return next(d for d in items if d["role"] == "elder")


def test_not_reported_is_null_and_no_warning(client):
    f = Family(client)
    assert client.get("/api/v1/me", headers=f.e).json()["device"]["notification_status"] is None
    assert _elder_device(client, f)["notification_status"] is None
    assert _warning(client, f.g) is False
    assert _warning(client, f.e) is None  # 어르신 응답에는 없음


def test_report_saved_and_visible(client):
    f = Family(client)
    at(23, 9, 0)
    res = _report(client, f.e, True, False)
    assert res.status_code == 200
    status = {
        "notifications_allowed": True,
        "exact_alarm_allowed": False,
        "ready": False,
        "reported_at": "2026-10-23T09:00:00+09:00",
    }
    assert res.json() == status
    assert client.get("/api/v1/me", headers=f.e).json()["device"]["notification_status"] == status
    assert _elder_device(client, f)["notification_status"] == status
    assert _warning(client, f.g) is True
    # 다시 허용하면 경고가 사라지고 보고 시각이 바뀐다
    at(23, 9, 5)
    assert _report(client, f.e, True, True).json()["ready"] is True
    assert _elder_device(client, f)["notification_status"]["reported_at"] == (
        "2026-10-23T09:05:00+09:00"
    )
    assert _warning(client, f.g) is False


def test_ready_rules(client):
    f = Family(client)
    cases = [
        ((True, None), True),
        ((True, True), True),
        ((True, False), False),
        ((False, None), False),
        ((False, True), False),
    ]
    for (allowed, exact), ready in cases:
        assert _report(client, f.e, allowed, exact).json()["ready"] is ready
        assert _warning(client, f.g) is (not ready)


def test_guardian_report_does_not_warn(client):
    f = Family(client)
    assert _report(client, f.g, False, False).status_code == 200
    assert _warning(client, f.g) is False


def test_revoked_elder_device_does_not_warn(client):
    f = Family(client)
    _report(client, f.e, False, None)
    assert _warning(client, f.g) is True
    elder = _elder_device(client, f)
    assert client.delete(f"/api/v1/devices/{elder['id']}", headers=f.g).status_code == 204
    assert _warning(client, f.g) is False


def test_allowed_before_consent(client):
    f = Family(client, consent=False)
    assert _report(client, f.e, True, True).status_code == 200


def test_validation(client):
    f = Family(client)
    for body in (
        {},
        {"notifications_allowed": True},
        {"notifications_allowed": None, "exact_alarm_allowed": None},
        {"notifications_allowed": "maybe", "exact_alarm_allowed": True},
    ):
        res = client.put(PATH, json=body, headers=f.e)
        assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error", body


def test_push_token_unchanged(client):
    f = Family(client)
    _report(client, f.e, False, False)
    res = client.put(
        "/api/v1/devices/me/push-token", json={"push_token": "ExponentPushToken[a]"}, headers=f.e
    )
    assert res.status_code == 204 and res.content == b""
    # push-token 은 알림 상태를 건드리지 않는다
    assert _elder_device(client, f)["notification_status"]["ready"] is False


def test_existing_db_gets_new_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    ensure_schema(engine)
    with engine.begin() as conn:
        for col in ("notifications_allowed", "exact_alarm_allowed", "notification_status_at"):
            conn.execute(text(f"ALTER TABLE devices DROP COLUMN {col}"))
    ensure_schema(engine)
    names = {c["name"] for c in inspect(engine).get_columns("devices")}
    assert {"notifications_allowed", "exact_alarm_allowed", "notification_status_at"} <= names
