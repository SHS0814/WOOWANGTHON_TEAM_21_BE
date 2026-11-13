"""판독 프롬프트 초안. 정확도 검증 하네스용이며, 확정 전에 메인 에이전트 검토를 받는다."""

from app.reader.base import CAMERA_ANGLES, Layout

# 2026-10-25 실사진 점검(T-AI1)으로 고침. 이유:
# - 실사진의 약은 알약이 아니라 접힌 처방 약봉투다. "약"을 알약으로만 읽지 않게 내용물 형태를 적었다
# - 실사진 41장은 잘라 낸 띠 사진이지만 대부분 7칸이 다 보이고, 사진에 안 보이는 칸은 초안 기준 350칸 중
#   9칸이다. 그래도 일부 칸만 찍히는 경우가 있으므로 칸 위치를 사진 속 순서가 아니라 칸 라벨(요일 글자)로
#   맞추고, 안 보이는 칸은 추측 대신 unclear 로 돌리게 했다 (원칙 5)
# - 약을 꺼낸 칸은 뚜껑이 열려 라벨이 안 보이기 쉽다(실사진 다수). 옆 칸 라벨로 위치를 정하게 했다
# - 세로로 놓이거나 거꾸로 찍힌 사진이 있다. 방향과 상관없이 라벨로 칸을 정하게 했다
# - 라벨이 한자(月火水…)일 수 있다. 라벨 글자와 투명 뚜껑 반사를 약으로 보지 않게 했다
# 2026-11-13 B 약통 60장 평가로 고침. 이유:
# - 낮은 각도로 비스듬히 찍은 사진에서 약이 반투명 뚜껑·칸막이에 가려 빈 칸처럼 보였고, 모델이 높은 신뢰도로
#   "empty"라고 답했다 (sol 위험 오류 14칸 중 12칸이 0.9 이상). 사진 각도(camera_angle)를 따로 답하게 하고,
#   칸 바닥이 다 보이지 않으면 unclear 로 돌리게 했다. 서버는 oblique 사진을 정상으로 기록하지 않는다
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
- 뚜껑이 열려 라벨이 안 보이는 칸도 있다. 약을 꺼내려고 연 칸일 수 있으니 꼭 판단해라. 바로 옆 칸들의
  라벨로 위치가 분명하면(예: 水와 金 사이의 칸) 그 칸으로 본다
- 사진에 약통 일부 칸만 보일 수 있다. 사진에 없거나, 잘려서 칸 안쪽이 안 보이거나, 라벨과 옆 칸으로도
  어느 칸인지 확인할 수 없는 칸은 "unclear"로 답해. 사진에 안 나온 칸의 상태를 짐작하지 마
- 약은 알약·캡슐일 수도 있고 접힌 약봉투(종이·비닐 포장)일 수도 있다. 형태와 상관없이 칸 안에 약이나
  약봉투가 있으면 약이 있는 것이다
- 칸 라벨 글자, 투명 뚜껑의 반사, 뚜껑 너머로 비치는 바닥 무늬는 약이 아니다

먼저 사진을 찍은 각도를 판단해.
- camera_angle: 약통을 위에서 내려다보고 찍어 모든 칸의 바닥이 보이면 "top". 옆이나 끝에서 낮은 각도로
  비스듬히 찍어 뚜껑·칸막이·앞쪽 칸에 가려 칸 바닥이 다 보이지 않는 칸이 하나라도 있으면 "oblique"
- 비스듬한 사진에서는 약이 칸막이나 반투명 뚜껑 뒤에 숨어 빈 칸처럼 보일 수 있다. 칸 바닥 전체가 보이지
  않는 칸은 약이 안 보여도 "empty"라고 답하지 말고 "unclear"로 답해
- 비스듬한 사진에서는 뚜껑의 라벨 글자와 그 아래 칸 바닥이 어긋나 보인다. 약이 어느 칸 안에 있는지는
  라벨 위치가 아니라 칸막이로 판단해

모든 칸에 대해 약이 들어 있는지 판단해.
- state: 약이 보이면 "full", 칸 바닥 전체가 보이고 확실히 비었으면 "empty", 안 보이거나 가려지거나 흐리거나
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
            "camera_angle": {"type": "string", "enum": list(CAMERA_ANGLES)},
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
            },
        },
        "required": ["camera_angle", "cells"],
        "additionalProperties": False,
    }
