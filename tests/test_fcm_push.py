"""FCM HTTP v1 직접 발송 (R-039, T-BE7). 실제 Google 서버 대신 httpx MockTransport."""

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import Settings
from app.models import Device
from app.push import (
    ExpoPushSender,
    FcmPushSender,
    PushMessage,
    RecordingPushSender,
    build_push_sender,
    clear_push_token,
)
from tests.test_dose import at, event_id, fam, open_handoffs  # noqa: F401 (fam: fixture)

TOKEN_URI = "https://oauth2.example/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/test-proj/messages:send"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sa_file(tmp_path, rsa_key):
    """테스트용 서비스 계정 파일 (그때그때 만든 키, 실제 자격 증명 아님)."""
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    p = tmp_path / "sa.json"
    p.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "test-proj",
                "client_email": "push@test-proj.iam.gserviceaccount.com",
                "private_key": pem,
                "token_uri": TOKEN_URI,
            }
        )
    )
    return str(p)


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def fcm_error(status: int, code: str, errorcode: str | None = None, field: str | None = None):
    details = []
    if errorcode:
        details.append(
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": errorcode}
        )
    if field:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [{"field": field, "description": "bad"}],
            }
        )
    return httpx.Response(
        status, json={"error": {"code": status, "status": code, "details": details}}
    )


class FakeFcm:
    """토큰 발급과 발송을 흉내 낸다. send_responses 로 발송 응답을 차례로 정한다."""

    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.sends: list[tuple[str, dict]] = []
        self.send_responses: list[httpx.Response] = []
        self.token_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URI:
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.token_requests.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": f"at-{len(self.token_requests)}", "expires_in": 3599},
            )
        assert str(request.url) == SEND_URL
        self.sends.append((request.headers["authorization"], json.loads(request.content)))
        if self.send_responses:
            return self.send_responses.pop(0)
        return httpx.Response(200, json={"name": "projects/test-proj/messages/1"})


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def make_sender(sa_file, fake, clock=None, forgotten=None, **kw) -> FcmPushSender:
    return FcmPushSender(
        sa_file,
        transport=httpx.MockTransport(fake),
        clock=clock or Clock(),
        forget_token=(lambda d, t: forgotten.append((d, t))) if forgotten is not None else None,
        **kw,
    )


def test_send_payload_and_signed_assertion(sa_file, rsa_key):
    fake = FakeFcm()
    sender = make_sender(sa_file, fake)
    sender.send(
        [
            PushMessage(
                "d1", "fcm-tok-1", "확인이 필요해요", "아침 약", {"type": "handoff", "n": 2}
            ),
            PushMessage("d2", None, "t", "b", {}),  # 토큰 없는 기기는 건너뜀
        ]
    )
    assert len(fake.sends) == 1 and [m.device_id for m in sender.sent] == ["d1"]
    auth, body = fake.sends[0]
    assert auth == "Bearer at-1"
    assert body == {
        "validate_only": False,
        "message": {
            "token": "fcm-tok-1",
            "notification": {"title": "확인이 필요해요", "body": "아침 약"},
            "data": {"type": "handoff", "n": "2"},  # 값은 모두 문자열
            "android": {
                "priority": "HIGH",
                "notification": {
                    "channel_id": "guardian-quiet",
                    "notification_priority": "PRIORITY_HIGH",
                    "visibility": "PUBLIC",
                },
            },
        },
    }
    # OAuth: 서비스 계정 키로 RS256 서명한 JWT
    (form,) = fake.token_requests
    assert form["grant_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
    h, c, s = form["assertion"].split(".")
    assert json.loads(_unb64(h)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unb64(c))
    assert claims["iss"] == "push@test-proj.iam.gserviceaccount.com"
    assert claims["aud"] == TOKEN_URI
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert claims["exp"] - claims["iat"] == 3600
    rsa_key.public_key().verify(_unb64(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_validate_only_and_channel_setting(sa_file):
    fake = FakeFcm()
    sender = make_sender(sa_file, fake, validate_only=True, channel_id="other")
    sender.send([PushMessage("d1", "tok", "t", "b", {})])
    body = fake.sends[0][1]
    assert body["validate_only"] is True
    assert body["message"]["android"]["notification"]["channel_id"] == "other"


def test_access_token_cached_until_near_expiry(sa_file):
    fake, clock = FakeFcm(), Clock()
    sender = make_sender(sa_file, fake, clock=clock)
    msg = [PushMessage("d1", "tok", "t", "b", {})]
    sender.send(msg)
    clock.t += 3000
    sender.send(msg)
    assert len(fake.token_requests) == 1
    clock.t += 560  # 만료(3599초) 1분 전을 넘김 → 새로 받음
    sender.send(msg)
    assert len(fake.token_requests) == 2
    assert [a for a, _ in fake.sends] == ["Bearer at-1", "Bearer at-1", "Bearer at-2"]


def test_401_refreshes_token_once(sa_file):
    fake = FakeFcm()
    fake.send_responses = [fcm_error(401, "UNAUTHENTICATED")]
    sender = make_sender(sa_file, fake)
    sender.send([PushMessage("d1", "tok", "t", "b", {})])
    assert [a for a, _ in fake.sends] == ["Bearer at-1", "Bearer at-2"]
    assert len(sender.sent) == 1


@pytest.mark.parametrize(
    "response",
    [
        fcm_error(404, "NOT_FOUND", "UNREGISTERED"),
        fcm_error(403, "PERMISSION_DENIED", "SENDER_ID_MISMATCH"),
        fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT", field="message.token"),
    ],
)
def test_invalid_token_is_forgotten(sa_file, response):
    fake, forgotten = FakeFcm(), []
    fake.send_responses = [response]
    sender = make_sender(sa_file, fake, forgotten=forgotten)
    sender.send([PushMessage("d1", "dead", "t", "b", {}), PushMessage("d2", "ok", "t", "b", {})])
    assert forgotten == [("d1", "dead")]
    assert [m.device_id for m in sender.sent] == ["d2"]  # 다음 기기는 계속 보냄


@pytest.mark.parametrize(
    "response",
    [
        fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT", field="message.data"),
        fcm_error(429, "RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"),
        fcm_error(503, "UNAVAILABLE", "UNAVAILABLE"),
        httpx.Response(500, text="oops"),
    ],
)
def test_other_errors_keep_token_and_do_not_raise(sa_file, response):
    fake, forgotten = FakeFcm(), []
    fake.send_responses = [response]
    sender = make_sender(sa_file, fake, forgotten=forgotten)
    sender.send([PushMessage("d1", "tok", "t", "b", {})])
    assert forgotten == [] and sender.sent == []


def test_token_endpoint_or_network_failure_does_not_raise(sa_file):
    fake = FakeFcm()
    fake.token_status = 400
    sender = make_sender(sa_file, fake)
    sender.send([PushMessage("d1", "tok", "t", "b", {})])
    assert sender.sent == [] and fake.sends == []

    def boom(request):
        raise httpx.ConnectError("down")

    sender = FcmPushSender(sa_file, transport=httpx.MockTransport(boom))
    sender.send([PushMessage("d1", "tok", "t", "b", {})])
    assert sender.sent == []


def test_forget_failure_does_not_raise(sa_file):
    def forget(d, t):
        raise RuntimeError("db down")

    fake = FakeFcm()
    fake.send_responses = [fcm_error(404, "NOT_FOUND", "UNREGISTERED")]
    sender = FcmPushSender(sa_file, transport=httpx.MockTransport(fake), forget_token=forget)
    sender.send([PushMessage("d1", "dead", "t", "b", {})])


def test_unregistered_clears_device_token_in_db(client, fam, sa_file):  # noqa: F811
    """인계 푸시가 UNREGISTERED 를 받으면 그 보호자 기기의 push_token 을 지운다."""
    assert (
        client.put(
            "/api/v1/devices/me/push-token", json={"push_token": "dead"}, headers=fam.g
        ).status_code
        == 204
    )
    fake = FakeFcm()
    fake.send_responses = [fcm_error(404, "NOT_FOUND", "UNREGISTERED")]
    app = client.app
    app.state.push = FcmPushSender(
        sa_file,
        transport=httpx.MockTransport(fake),
        forget_token=clear_push_token(app.state.sessionmaker),
    )
    at(23, 8, 30)  # 무응답 인계
    event_id(client, fam, "morning")
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["no_response"]
    assert fake.sends[0][1]["message"]["token"] == "dead"
    assert fake.sends[0][1]["message"]["data"]["reason"] == "no_response"
    with app.state.sessionmaker() as db:
        tokens = [d.push_token for d in db.query(Device).filter_by(role="guardian")]
    assert tokens == [None]


def test_clear_push_token_keeps_newer_token(client, fam):  # noqa: F811
    """지우기 전에 앱이 새 토큰을 등록했으면 새 토큰은 남긴다."""
    client.put("/api/v1/devices/me/push-token", json={"push_token": "new"}, headers=fam.g)
    sm = client.app.state.sessionmaker
    with sm() as db:
        gid = db.query(Device).filter_by(role="guardian").one().id
    clear_push_token(sm)(gid, "old")
    with sm() as db:
        assert db.get(Device, gid).push_token == "new"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_build_push_sender_selection(sa_file):
    assert isinstance(build_push_sender(_settings(), None), RecordingPushSender)  # 기본은 그대로
    assert isinstance(build_push_sender(_settings(expo_push_enabled=True), None), ExpoPushSender)
    assert isinstance(build_push_sender(_settings(push_provider="expo"), None), ExpoPushSender)
    assert isinstance(
        build_push_sender(_settings(push_provider="recording", expo_push_enabled=True), None),
        RecordingPushSender,
    )
    s = build_push_sender(_settings(push_provider="FCM", fcm_service_account_file=sa_file), None)
    assert isinstance(s, FcmPushSender)


@pytest.mark.parametrize("content", [None, "not json", '{"project_id": "p"}', "BAD_KEY"])
def test_build_push_sender_bad_fcm_file_logs_error(tmp_path, caplog, content, sa_file):
    path = tmp_path / "x.json"
    if content == "BAD_KEY":
        info = json.loads(Path(sa_file).read_text())
        info["private_key"] = "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"
        path.write_text(json.dumps(info))
    elif content is not None:
        path.write_text(content)
    with caplog.at_level(logging.ERROR, logger="app.push"):
        s = build_push_sender(
            _settings(push_provider="fcm", fcm_service_account_file=str(path)), None
        )
    assert isinstance(s, RecordingPushSender)
    assert any(r.levelno == logging.ERROR and "FCM" in r.getMessage() for r in caplog.records)
    assert "nope" not in caplog.text  # 키 내용은 로그에 남기지 않는다


def test_build_push_sender_fcm_without_file_or_unknown(caplog):
    with caplog.at_level(logging.ERROR, logger="app.push"):
        assert isinstance(
            build_push_sender(_settings(push_provider="fcm"), None), RecordingPushSender
        )
        assert isinstance(
            build_push_sender(_settings(push_provider="apns"), None), RecordingPushSender
        )
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2


def test_server_starts_with_missing_fcm_file(settings, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.jobs_enabled = False
    settings.push_provider = "fcm"
    settings.fcm_service_account_file = str(tmp_path / "missing.json")
    client = TestClient(create_app(settings))
    assert client.get("/health").status_code == 200
    assert isinstance(client.app.state.push, RecordingPushSender)
