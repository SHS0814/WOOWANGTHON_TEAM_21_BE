"""OpenAIReader 응답 파싱 (네트워크 없음, httpx.MockTransport)."""

import json

import httpx
import pytest

from app.config import Settings
from app.reader import Layout, ReaderError, ReaderUnavailable, build_reader
from app.reader.openai import OpenAIReader, strict_schema
from app.reader.prompts import cells_prompt, layout_schema

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
LAYOUT = Layout(rows=1, cols=3, row_labels=[""], col_labels=["月", "火", "水"])


def _response(payload: dict | str, status: str = "completed", **extra) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "status": status,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {"input_tokens": 1200, "output_tokens": 80},
        **extra,
    }


def _reader(handler) -> tuple[OpenAIReader, list[dict]]:
    sent: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.url.path == "/v1/responses"
        return handler(request)

    return OpenAIReader("sk-test", "gpt-test", transport=httpx.MockTransport(wrapped)), sent


def test_unavailable_without_key():
    with pytest.raises(ReaderUnavailable, match="OPENAI_API_KEY"):
        build_reader("openai", Settings(_env_file=None, openai_api_key=None))


def test_build_reader_uses_model_from_spec():
    reader = build_reader("openai:gpt-x", Settings(_env_file=None, openai_api_key="sk-test"))
    assert (reader.provider, reader.model) == ("openai", "gpt-x")


def test_read_cells_parses_and_sends_shared_prompt():
    cells = {
        "cells": [
            {"cell_id": "r0c0", "state": "full", "confidence": 0.93},
            {"cell_id": "r0c2", "state": "empty", "confidence": 1.7},
        ]
    }
    reader, sent = _reader(lambda r: httpx.Response(200, json=_response(cells)))
    out = reader.read_cells(PNG, LAYOUT)

    assert out.provider == "openai" and out.model == "gpt-test" and out.is_mock is False
    assert [(c.cell_id, c.state) for c in out.cells] == [
        ("r0c0", "full"),
        ("r0c1", "unclear"),  # 빠진 칸은 unclear
        ("r0c2", "empty"),
    ]
    assert out.cells[2].confidence == 1.0  # 0~1 로 자름
    assert (out.input_tokens, out.output_tokens) == (1200, 80)
    body = sent[0]
    content = body["input"][0]["content"]
    assert content[0]["image_url"].startswith("data:image/png;base64,")
    assert content[1]["text"] == cells_prompt(LAYOUT)  # Claude·Ollama 와 같은 프롬프트
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert fmt["schema"]["properties"]["cells"]["items"]["properties"]["cell_id"]["enum"] == [
        "r0c0",
        "r0c1",
        "r0c2",
    ]


def test_detect_layout_parses():
    layout = {
        "rows": 1,
        "cols": 7,
        "row_labels": [""],
        "col_labels": list("月火水木金土日"),
        "suggested_time_of_day": [None],
        "uncertain": False,
    }
    reader, _ = _reader(lambda r: httpx.Response(200, json=_response(layout)))
    prop = reader.detect_layout(PNG)
    assert (prop.layout.rows, prop.layout.cols) == (1, 7)
    assert prop.layout.col_labels[0] == "月"
    assert prop.input_tokens == 1200


def test_refusal_is_reader_error():
    res = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
    }
    reader, _ = _reader(lambda r: httpx.Response(200, json=res))
    with pytest.raises(ReaderError, match="refusal"):
        reader.read_cells(PNG, LAYOUT)


def test_incomplete_and_invalid_json_are_reader_errors():
    reader, _ = _reader(
        lambda r: httpx.Response(
            200,
            json=_response("{", status="incomplete", incomplete_details={"reason": "max_tokens"}),
        )
    )
    with pytest.raises(ReaderError, match="max_tokens"):
        reader.read_cells(PNG, LAYOUT)
    reader, _ = _reader(lambda r: httpx.Response(200, json=_response("not json")))
    with pytest.raises(ReaderError, match="invalid json"):
        reader.read_cells(PNG, LAYOUT)


def test_server_error_retries_once_then_fails(monkeypatch):
    monkeypatch.setattr("app.reader.openai.time.sleep", lambda s: None)
    reader, sent = _reader(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(ReaderError, match="500: boom"):
        reader.read_cells(PNG, LAYOUT)
    assert len(sent) == 2


def test_client_error_does_not_retry():
    reader, sent = _reader(lambda r: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(ReaderError, match="400: bad"):
        reader.read_cells(PNG, LAYOUT)
    assert len(sent) == 1


def test_strict_schema_types_nullable_enum():
    items = strict_schema(layout_schema())["properties"]["suggested_time_of_day"]["items"]
    assert items["type"] == ["null", "string"]
    assert "type" not in layout_schema()["properties"]["suggested_time_of_day"]["items"]


def test_external_transfer_includes_openai():
    s = Settings(_env_file=None, reader_layout="mock", reader_cells="openai:gpt-x")
    assert s.ai_external_transfer is True
