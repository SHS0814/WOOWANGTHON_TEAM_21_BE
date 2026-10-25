from datetime import timedelta

from app.clock import clock
from tests.conftest import Family, bearer


def test_create_family_and_me(client):
    res = client.post(
        "/api/v1/families",
        json={"elder_display_name": "어머니", "guardian_display_name": "민수", "platform": "ios"},
    )
    assert res.status_code == 201
    body = res.json()
    assert len(body["token"]) == 43
    assert body["device"]["role"] == "guardian" and body["device"]["is_me"]
    me = client.get("/api/v1/me", headers=bearer(body["token"])).json()
    assert me["family"]["elder_display_name"] == "어머니"
    assert me["family"]["guardians"] == [
        {"device_id": body["device"]["id"], "display_name": "민수"}
    ]
    assert me["family"]["is_demo"] is False and me["family"]["has_pillbox"] is False
    assert me["server"]["timing"] == {
        "reask_offsets_min": [10, 20],
        "no_response_handoff_min": 30,
        "retake_wait_min": 15,
    }
    assert me["server"]["reader_is_mock"] is True
    assert me["server"]["demo_mode"] is False
    assert me["device"]["paired_at"].endswith("+09:00")


def test_demo_family_needs_demo_mode(client):
    res = client.post(
        "/api/v1/families",
        json={"elder_display_name": "어머니", "guardian_display_name": "민수", "is_demo": True},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"


def test_auth_errors(client):
    assert client.get("/api/v1/me").json()["error"]["code"] == "unauthorized"
    res = client.get("/api/v1/me", headers=bearer("x" * 43))
    assert res.status_code == 401 and res.json()["error"]["code"] == "unauthorized"


def test_elder_pairing_consent_flow(client):
    f = Family(client, consent=False)
    me = client.get("/api/v1/me", headers=f.e).json()
    assert me["device"]["role"] == "elder" and me["consent_required"] is True
    # 동의 전에는 막힘
    res = client.get("/api/v1/devices", headers=f.e)
    assert res.status_code == 403 and res.json()["error"]["code"] == "consent_required"
    # push-token 은 동의 전에도 가능
    res = client.put(
        "/api/v1/devices/me/push-token", json={"push_token": "ExponentPushToken[a]"}, headers=f.e
    )
    assert res.status_code == 204
    res = client.post(
        "/api/v1/consent",
        json={"agreed": True, "consent_text_version": "v1", "ai_external_transfer_shown": False},
        headers=f.e,
    )
    assert res.status_code == 200 and res.json()["consent_at"]
    # 동의 후에는 역할 검사로 넘어간다. 어르신은 보호자 전용 API를 못 씀
    res = client.get("/api/v1/settings", headers=f.e)
    assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden_role"


def test_consent_requires_ai_notice_when_external(client, settings):
    settings.reader_cells = "claude"
    f = Family(client, consent=False)
    res = client.post(
        "/api/v1/consent",
        json={"agreed": True, "consent_text_version": "v1", "ai_external_transfer_shown": False},
        headers=f.e,
    )
    assert res.status_code == 422 and res.json()["error"]["code"] == "consent_incomplete"


def test_new_elder_device_revokes_old(client):
    f = Family(client)
    code = client.post("/api/v1/pairing-codes", json={"kind": "elder"}, headers=f.g).json()
    res = client.post("/api/v1/pair", json={"short_code": code["short_code"]})
    assert res.status_code == 201
    assert res.json()["consent_required"] is True
    old = client.get("/api/v1/me", headers=f.e)
    assert old.status_code == 401 and old.json()["error"]["code"] == "token_revoked"


def test_code_reuse_expiry_and_invalid(client):
    f = Family(client)
    code = client.post("/api/v1/pairing-codes", json={"kind": "elder"}, headers=f.g).json()
    assert code["qr_payload"].endswith(code["code"]) and len(code["short_code"]) == 6
    # 새 코드를 발급하면 이전 코드는 무효
    newer = client.post("/api/v1/pairing-codes", json={"kind": "elder"}, headers=f.g).json()
    res = client.post("/api/v1/pair", json={"code": code["code"]})
    assert res.status_code == 410 and res.json()["error"]["code"] == "code_used"
    clock.set(clock.now() + timedelta(minutes=11))
    res = client.post("/api/v1/pair", json={"code": newer["code"]})
    assert res.status_code == 410 and res.json()["error"]["code"] == "code_expired"
    res = client.post("/api/v1/pair", json={"code": "nope"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "code_invalid"


def test_pair_failures_rate_limited_per_ip(client):
    for _ in range(5):
        assert client.post("/api/v1/pair", json={"short_code": "000000"}).status_code == 400
    res = client.post("/api/v1/pair", json={"short_code": "000000"})
    assert res.status_code == 429 and res.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in res.headers


def test_guardian_invite_notifies_existing_guardians(client, push):
    f = Family(client)
    code = client.post("/api/v1/pairing-codes", json={"kind": "guardian"}, headers=f.g).json()
    assert code["short_code"] is None
    res = client.post("/api/v1/pair", json={"code": code["code"]})
    assert res.status_code == 422  # 이름 필요
    res = client.post("/api/v1/pair", json={"code": code["code"], "display_name": "민지"})
    assert res.status_code == 201 and res.json()["consent_required"] is False
    names = [g["display_name"] for g in res.json()["family"]["guardians"]]
    assert names == ["민수", "민지"]
    assert [m.data for m in push.sent] == [
        {"type": "guardian_joined", "device_id": res.json()["device"]["id"]}
    ]
    assert push.sent[0].body == "민지님이 보호자로 추가됐어요"


def test_devices_and_last_guardian(client):
    f = Family(client)
    items = client.get("/api/v1/devices", headers=f.g).json()["items"]
    assert {d["role"] for d in items} == {"guardian", "elder"}
    me = next(d for d in items if d["is_me"])
    res = client.delete(f"/api/v1/devices/{me['id']}", headers=f.g)
    assert res.status_code == 409 and res.json()["error"]["code"] == "last_guardian"
    elder = next(d for d in items if d["role"] == "elder")
    assert client.delete(f"/api/v1/devices/{elder['id']}", headers=f.g).status_code == 204
    assert client.get("/api/v1/me", headers=f.e).json()["error"]["code"] == "token_revoked"


def test_other_family_is_not_found(client):
    a, b = Family(client), Family(client)
    elder_b = next(
        d
        for d in client.get("/api/v1/devices", headers=b.g).json()["items"]
        if d["role"] == "elder"
    )
    res = client.delete(f"/api/v1/devices/{elder_b['id']}", headers=a.g)
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


def test_settings_get_put(client):
    f = Family(client)
    s = client.get("/api/v1/settings", headers=f.g).json()
    assert s == {
        "elder_display_name": "어머니",
        "summary_push_enabled": False,
        "summary_push_time": "21:00",
        "photo_retention_days": 90,
        "unchanged_mode": "remind_then_retake",
    }
    res = client.put(
        "/api/v1/settings",
        json={"unchanged_mode": "handoff_immediately", "elder_display_name": "엄마"},
        headers=f.g,
    )
    assert res.json()["unchanged_mode"] == "handoff_immediately"
    assert client.get("/api/v1/me", headers=f.e).json()["device"]["display_name"] == "엄마"
    res = client.put("/api/v1/settings", json={"summary_push_time": "25:00"}, headers=f.g)
    assert res.status_code == 422


def test_blank_names_are_rejected_after_strip(client):
    """R-021 Q-002: 공백뿐인 이름은 strip 뒤 길이 검증에서 걸린다. 앞뒤 공백은 지워서 저장."""
    for body in (
        {"elder_display_name": "   ", "guardian_display_name": "민수"},
        {"elder_display_name": "어머니", "guardian_display_name": " \t "},
    ):
        res = client.post("/api/v1/families", json=body)
        assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error"
    res = client.post(
        "/api/v1/families",
        json={"elder_display_name": "  어머니 ", "guardian_display_name": " 민수"},
    )
    assert res.status_code == 201 and res.json()["device"]["display_name"] == "민수"
    f = Family(client)
    code = client.post("/api/v1/pairing-codes", json={"kind": "guardian"}, headers=f.g).json()
    res = client.post("/api/v1/pair", json={"code": code["code"], "display_name": "   "})
    assert res.status_code == 422
    res = client.put("/api/v1/settings", json={"elder_display_name": "  "}, headers=f.g)
    assert res.status_code == 422
    res = client.put("/api/v1/settings", json={"elder_display_name": " 엄마 "}, headers=f.g)
    assert res.json()["elder_display_name"] == "엄마"
