"""판독 프롬프트 초안. 정확도 검증 하네스용이며, 확정 전에 메인 에이전트 검토를 받는다."""

from app.reader.base import Layout

# 2026-10-25 실사진 점검(T-AI1)으로 고침. 이유:
# - 실사진의 약은 알약이 아니라 접힌 처방 약봉투다. "약"을 알약으로만 읽지 않게 내용물 형태를 적었다
# - 어르신은 약통 일부 칸만 찍는다(실사진 41/50장). 칸 위치를 사진 속 순서가 아니라 칸 라벨(요일 글자)로
#   맞추고, 안 보이는 칸은 추측 대신 unclear 로 돌리게 했다 (원칙 5)
# - 세로로 놓이거나 거꾸로 찍힌 사진이 있다. 방향과 상관없이 라벨로 칸을 정하게 했다
# - 라벨이 한자(月火水…)일 수 있다. 라벨 글자와 투명 뚜껑 반사를 약으로 보지 않게 했다
LAYOUT_PROMPT = """\
사진은 요일별 약통이다. 약통의 칸 배열을 알려줘.
- 약통은 가로·세로로 놓였거나 거꾸로 찍혔을 수 있다. 사진 속 방향이 아니라 칸 라벨 글자를 읽는 순서
  (예: 월→일, 아침→저녁)를 기준으로 행과 열을 정해라
- rows, cols: 칸 격자의 행·열 수
- row_labels, col_labels: 약통에 적힌 라벨을 읽는 순서대로 그대로 적어라 (한자면 한자로). 안 보이면 빈 문자열
- suggested_time_of_day: 행마다 시간대(morning/noon/evening/bedtime). 행이 시간대가 아니거나 모르면 null
- uncertain: 약통 일부만 보이거나 배열을 확신하지 못하면 true
"""

CELLS_PROMPT = """\
사진은 요일별 약통이다. 약통 전체는 {rows}행 {cols}열 격자다.
행 라벨: {row_labels}
열 라벨: {col_labels}
칸 id는 "r{{행}}c{{열}}" 형식이고 0부터 센다. r0c0 은 첫 번째 행 라벨과 첫 번째 열 라벨이 만나는 칸이다.

사진을 볼 때:
- 약통은 가로·세로로 놓였거나 거꾸로 찍혔을 수 있다. 칸 위치는 사진 속 순서가 아니라 칸에 적힌 라벨 글자
  (요일 등. 한자일 수 있다)로 맞춰라
- 사진에 약통 일부 칸만 보일 수 있다. 사진에 없거나, 잘려서 칸 안쪽이 안 보이거나, 라벨로 어느 칸인지
  확인할 수 없는 칸은 "unclear"로 답해. 보이는 칸의 순서로 안 보이는 칸을 짐작하지 마
- 약은 알약·캡슐일 수도 있고 접힌 약봉투(종이·비닐 포장)일 수도 있다. 형태와 상관없이 칸 안에 약이나
  약봉투가 있으면 약이 있는 것이다
- 칸 라벨 글자, 투명 뚜껑의 반사, 뚜껑 너머로 비치는 바닥 무늬는 약이 아니다

모든 칸에 대해 약이 들어 있는지 판단해.
- state: 약이 보이면 "full", 칸 안쪽이 보이고 확실히 비었으면 "empty", 안 보이거나 가려지거나 흐리거나
  반사 때문에 판단할 수 없으면 "unclear"
- 추측하지 마. 확실하지 않으면 "unclear"로 답해
- confidence: 0~1 사이의 확신 정도
판독할 칸: {cell_ids}
"""


def cells_prompt(layout: Layout) -> str:
    """모든 판독기가 같은 문장을 쓰도록 여기서만 만든다."""
    return CELLS_PROMPT.format(
        rows=layout.rows,
        cols=layout.cols,
        row_labels=", ".join(layout.row_labels) or "(없음)",
        col_labels=", ".join(layout.col_labels) or "(없음)",
        cell_ids=", ".join(layout.cell_ids()),
    )


def layout_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "rows": {"type": "integer"},
            "cols": {"type": "integer"},
            "row_labels": {"type": "array", "items": {"type": "string"}},
            "col_labels": {"type": "array", "items": {"type": "string"}},
            "suggested_time_of_day": {
                "type": "array",
                "items": {"enum": ["morning", "noon", "evening", "bedtime", None]},
            },
            "uncertain": {"type": "boolean"},
        },
        "required": [
            "rows",
            "cols",
            "row_labels",
            "col_labels",
            "suggested_time_of_day",
            "uncertain",
        ],
        "additionalProperties": False,
    }


def cells_schema(cell_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "cells": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "cell_id": {"type": "string", "enum": cell_ids},
                        "state": {"type": "string", "enum": ["empty", "full", "unclear"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["cell_id", "state", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cells"],
        "additionalProperties": False,
    }
