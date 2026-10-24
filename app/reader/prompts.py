"""판독 프롬프트 초안. 정확도 검증 하네스용이며, 확정 전에 메인 에이전트 검토를 받는다."""

LAYOUT_PROMPT = """\
사진은 요일별 약통이다. 약통의 칸 배열을 알려줘.
- rows, cols: 칸 격자의 행·열 수
- row_labels, col_labels: 약통에 적힌 라벨을 위에서 아래, 왼쪽에서 오른쪽 순서로. 안 보이면 빈 문자열
- suggested_time_of_day: 행마다 시간대(morning/noon/evening/bedtime). 행이 시간대가 아니거나 모르면 null
- uncertain: 배열을 확신하지 못하면 true
"""

CELLS_PROMPT = """\
사진은 요일별 약통이다. 약통은 {rows}행 {cols}열 격자다.
행 라벨: {row_labels}
열 라벨: {col_labels}
칸 id는 "r{{행}}c{{열}}" 형식이고 0부터 센다 (왼쪽 위가 r0c0).

모든 칸에 대해 약이 들어 있는지 판단해.
- state: 약이 하나라도 보이면 "full", 확실히 비었으면 "empty", 가려지거나 흐리거나 반사 때문에 판단할 수 없으면 "unclear"
- 추측하지 마. 확실하지 않으면 "unclear"로 답해
- confidence: 0~1 사이의 확신 정도
판독할 칸: {cell_ids}
"""


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
