"""사진 각도(camera_angle): 비스듬히 찍은 사진은 칸 판독과 상관없이 애매로 돌린다 (2026-11-13 B 약통 평가)."""

import json

import httpx

from app.reader import Layout
from app.reader.base import CellReading, CellReadings
from app.reader.openai import OpenAIReader
from app.reader.prompts import cells_schema
from app.services.classify import classify

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
LAYOUT = Layout(rows=1, cols=3, row_labels=[""], col_labels=["Sun", "Mon", "Tue"])
EXPECTED = {"r0c0": "full", "r0c1": "full", "r0c2": "full"}


def _readings(states: dict[str, str], angle: str | None) -> CellReadings:
    return CellReadings(
        provider="x",
        model="x",
        is_mock=False,
        camera_angle=angle,
        cells=[CellReading(cell_id=k, state=v, confidence=0.99) for k, v in states.items()],
    )


def test_oblique_photo_is_never_normal():
    # 대상 칸만 비어 보이고 신뢰도도 높지만, 비스듬한 사진이면 정상으로 기록하지 않는다
    states = {"r0c0": "empty", "r0c1": "full", "r0c2": "full"}
    assert classify(_readings(states, "top"), EXPECTED, "r0c0", 0.9).kind == "normal"
    assert classify(_readings(states, None), EXPECTED, "r0c0", 0.9).kind == "normal"
    assert classify(_readings(states, "oblique"), EXPECTED, "r0c0", 0.9).kind == "unclear"


def test_oblique_photo_does_not_raise_double_dose_alarm():
    # 다른 칸까지 비어 보여도, 비스듬한 사진이면 이중 복용 의심 대신 다시 찍기
    states = {"r0c0": "empty", "r0c1": "empty", "r0c2": "full"}
    assert classify(_readings(states, "top"), EXPECTED, "r0c0", 0.9).kind == "double_suspected"
    assert classify(_readings(states, "oblique"), EXPECTED, "r0c0", 0.9).kind == "unclear"


def test_oblique_photo_overrides_already_used():
    expected = {**EXPECTED, "r0c0": "empty"}
    states = {"r0c0": "empty", "r0c1": "full", "r0c2": "full"}
    assert classify(_readings(states, "top"), expected, "r0c0", 0.9).kind == "already_used"
    assert classify(_readings(states, "oblique"), expected, "r0c0", 0.9).kind == "unclear"


def test_schema_requires_camera_angle():
    schema = cells_schema(LAYOUT.cell_ids())
    assert schema["properties"]["camera_angle"]["enum"] == ["top", "oblique"]
    assert "camera_angle" in schema["required"]


def _reader(payload: dict) -> OpenAIReader:
    body = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    return OpenAIReader("sk-test", "gpt-test", transport=transport)


def test_openai_reader_parses_camera_angle():
    cells = [{"cell_id": "r0c0", "state": "empty", "confidence": 0.95}]
    assert (
        _reader({"camera_angle": "oblique", "cells": cells}).read_cells(PNG, LAYOUT).camera_angle
        == "oblique"
    )
    assert (
        _reader({"camera_angle": "top", "cells": cells}).read_cells(PNG, LAYOUT).camera_angle
        == "top"
    )
    # 없거나 모르는 값이면 None (기존 동작 그대로)
    assert _reader({"cells": cells}).read_cells(PNG, LAYOUT).camera_angle is None
    assert (
        _reader({"camera_angle": "sideways", "cells": cells}).read_cells(PNG, LAYOUT).camera_angle
        is None
    )
