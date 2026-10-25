"""OpenAIReader 를 서버에 꽂은 전체 흐름 (T-AI2). 네트워크 대신 가짜 OpenAI 응답을 쓴다.

등록(detect → pillboxes → 기준선) → 복약 사진 → 정상·칸 그대로·애매 처리와,
판독 실패(시간 초과, 거절, 잘못된 JSON, 서버 오류, 크레딧 없음, 키 없음)가 서버를 죽이거나
정상으로 처리되지 않고 재촬영 → 보호자 인계로 가는지 (원칙 5) 본다.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.reader import build_server_reader
from app.reader.openai import OpenAIReader
from tests.conftest import Family, week_schedule
from tests.test_dose import at, event_id, open_handoffs, upload


class FakeOpenAI:
    """Responses API 흉내. mode 로 실패를 고르고, states 로 칸 상태를 정한다."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.default = "full"
        self.states: dict[str, str] = {}
        self.confidence = 0.95
        self.calls = 0

    def _text(self, body: dict) -> str:
        fmt = body["text"]["format"]
        if fmt["name"] == "pillbox_layout":
            return json.dumps(
                {
                    "rows": 2,
                    "cols": 7,
                    "row_labels": ["아침", "저녁"],
                    "col_labels": list("월화수목금토일"),
                    "suggested_time_of_day": ["morning", "evening"],
                    "uncertain": False,
                }
            )
        ids = fmt["schema"]["properties"]["cells"]["items"]["properties"]["cell_id"]["enum"]
        return json.dumps(
            {
                "cells": [
                    {
                        "cell_id": cid,
                        "state": self.states.get(cid, self.default),
                        "confidence": self.confidence,
                    }
                    for cid in ids
                ]
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "server_error":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if self.mode == "quota":
            return httpx.Response(
                429, json={"error": {"code": "insufficient_quota", "message": "no credits"}}
            )
        if self.mode == "garbage":
            return httpx.Response(200, text="<html>proxy error</html>")
        if self.mode == "refusal":
            content = [{"type": "refusal", "refusal": "no"}]
        else:
            text = "{not json" if self.mode == "bad_json" else self._text(body)
            content = [{"type": "output_text", "text": text}]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": content}],
                "usage": {"input_tokens": 900, "output_tokens": 120},
            },
        )


@pytest.fixture
def fake() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture
def client(settings, push, now, fake) -> TestClient:
    settings.jobs_enabled = False
    settings.reader_layout = settings.reader_cells = "openai"
    reader = OpenAIReader("sk-test", "gpt-test", timeout_s=5, transport=httpx.MockTransport(fake))
    return TestClient(create_app(settings, push=push, layout_reader=reader, cells_reader=reader))


def family(client) -> Family:
    """외부 AI 판독기를 쓰면 동의 때 외부 전송 안내를 보여 줬다고 보내야 한다."""
    f = Family(client, consent=False)
    res = client.post(
        "/api/v1/consent",
        json={"agreed": True, "consent_text_version": "v1", "ai_external_transfer_shown": True},
        headers=f.e,
    )
    assert res.status_code == 200, res.text
    return f


@pytest.fixture
def fam(client):
    f = family(client)
    f.register_pillbox()  # 가짜 OpenAI 기본값 full → 기준선 전부 full
    f.put_schedule(week_schedule())
    return f


def test_real_reader_flow_has_no_mock_marks(client, fake):
    f = family(client)
    me = client.get("/api/v1/me", headers=f.g).json()
    assert me["server"]["reader_is_mock"] is False
    assert me["server"]["ai_external_transfer"] is True  # 동의 화면에 외부 AI 전송 문구
    res = client.post(
        "/api/v1/pillboxes/detect",
        files={"photo": ("box.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
        headers=f.g,
    )
    assert res.status_code == 200, res.text
    assert res.json()["is_mock"] is False and res.json()["provider"] == "openai"
    f.register_pillbox()
    f.put_schedule(week_schedule())

    at(23, 8, 5)  # 금요일 아침 = r0c4
    fake.states = {"r0c4": "empty"}
    res = upload(client, f, event_id(client, f, "morning")).json()
    assert res["result"] == "recorded" and res["is_mock"] is False
    assert res["dose_event"]["state"] == "done"


def test_unchanged_then_unclear_paths(client, fam, fake):
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    fake.states = {}  # 이번 칸이 그대로 차 있음
    assert upload(client, fam, eid).json()["result"] == "remind_retake"
    at(23, 8, 10)
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    assert [h["reason"] for h in open_handoffs(client, fam)] == ["unchanged"]

    at(23, 19, 5)  # 저녁 = r1c4. 일부 칸이 안 보이면(unclear) 애매 → 재촬영
    eid = event_id(client, fam, "evening")
    fake.states = {"r1c4": "empty", "r0c0": "unclear"}
    assert upload(client, fam, eid).json()["result"] == "retake"


def test_low_confidence_is_not_recorded(client, fam, fake, settings):
    at(23, 8, 5)
    fake.states = {"r0c4": "empty"}
    fake.confidence = settings.normal_confidence - 0.05
    assert upload(client, fam, event_id(client, fam, "morning")).json()["result"] == "retake"


@pytest.mark.parametrize(
    "mode", ["timeout", "refusal", "bad_json", "server_error", "quota", "garbage"]
)
def test_reader_failures_retake_then_hand_off(client, fam, fake, mode):
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    fake.mode = mode
    fake.calls = 0
    res = upload(client, fam, eid)
    assert res.status_code == 200, res.text
    assert res.json()["result"] == "retake" and res.json()["is_mock"] is False
    # 시간 초과·크레딧 없음·거절은 다시 시도하지 않는다. 5xx 만 1회 재시도
    assert fake.calls == (2 if mode == "server_error" else 1)
    at(23, 8, 8)
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
    hs = open_handoffs(client, fam)
    assert [h["reason"] for h in hs] == ["unclear"]


def test_detect_failure_is_502_not_crash(client, fake):
    f = family(client)
    fake.mode = "bad_json"
    res = client.post(
        "/api/v1/pillboxes/detect",
        files={"photo": ("box.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
        headers=f.g,
    )
    assert res.status_code == 502 and res.json()["error"]["code"] == "reading_failed"


def test_missing_key_server_starts_and_readings_are_unclear(client, fam, settings, push):
    # 키 없이 openai 로 설정해도 서버는 뜬다
    no_key = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        photo_dir=settings.photo_dir,
        reader_layout="openai",
        reader_cells="openai",
        openai_api_key=None,
        jobs_enabled=False,
    )
    app = create_app(no_key, push=push)
    assert TestClient(app).get("/health").status_code == 200
    # 이미 등록된 가족의 판독기가 키 없는 판독기로 바뀌어도 애매 → 재촬영 → 인계
    client.app.state.cells_reader = build_server_reader("openai", no_key)
    at(23, 8, 5)
    eid = event_id(client, fam, "morning")
    res = upload(client, fam, eid).json()
    assert res["result"] == "retake" and res["is_mock"] is False
    at(23, 8, 8)
    assert upload(client, fam, eid).json()["result"] == "guardian_notified"
