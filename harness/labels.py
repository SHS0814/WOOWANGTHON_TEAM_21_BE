"""정확도 검증용 라벨 형식.

라벨 파일(JSON) 하나에 사진 여러 장을 적는다. 사진 경로는 라벨 파일 기준 상대 경로다.

{
  "version": 1,
  "drafted_by": "",                    # (선택) 초안을 누가 만들었는지
  "photos": [
    {
      "id": "home-0001",
      "file": "photos/home-0001.jpg",
      "pillbox": {                       # 사진 속 약통 구조 (app.reader.base.Layout 과 같은 형식)
        "name": "다이소 2x7",
        "layout": {"rows": 2, "cols": 7, "row_labels": ["아침", "저녁"],
                   "col_labels": ["월", "화", "수", "목", "금", "토", "일"]}
      },
      "cells": {"r0c0": "empty", "r0c1": "full", "r1c6": "not_visible", ...},
                                         # 칸별 정답. 모든 칸 필수.
                                         # empty | full | not_visible (사진에 안 보이거나 잘린 칸)
      "label_status": "confirmed",       # confirmed(사람이 확인함, 기본값) | draft(확인 전 초안)
      "check_cells": ["r0c0"],           # (선택) 초안 작성자가 확신하지 못한 칸. 확인 시트에서 강조
      "target_cell_id": "r0c1",          # (선택) 이번 복약 칸. 분석 때 따로 집계한다
      "conditions": {"lighting": "indoor_dim", "angle": "top", "lid": "open"},  # (선택) 촬영 조건
      "notes": ""
    }
  ]
}

not_visible 칸은 정확도 지표에서 빼고, 판독기가 그 칸을 애매(unclear)로 돌렸는지만 따로 센다.
draft 라벨은 평가 숫자의 근거가 될 수 없다. 하네스는 --allow-draft 없이는 실행을 거부한다.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.reader.base import Layout

Truth = Literal["empty", "full", "not_visible"]
LabelStatus = Literal["confirmed", "draft"]


class PillboxLabel(BaseModel):
    name: str = ""
    layout: Layout


class PhotoLabel(BaseModel):
    id: str
    file: str
    pillbox: PillboxLabel
    cells: dict[str, Truth]
    label_status: LabelStatus = "confirmed"
    check_cells: list[str] = Field(default_factory=list)
    target_cell_id: str | None = None
    conditions: dict[str, str | bool] = Field(default_factory=dict)
    notes: str = ""

    @model_validator(mode="after")
    def _check_cells(self) -> "PhotoLabel":
        expected = set(self.pillbox.layout.cell_ids())
        got = set(self.cells)
        if expected != got:
            raise ValueError(
                f"{self.id}: label cells mismatch layout "
                f"(missing={sorted(expected - got)}, extra={sorted(got - expected)})"
            )
        if self.target_cell_id is not None and self.target_cell_id not in expected:
            raise ValueError(f"{self.id}: target_cell_id {self.target_cell_id} not in layout")
        if not set(self.check_cells) <= expected:
            raise ValueError(f"{self.id}: check_cells not in layout")
        return self


class LabelSet(BaseModel):
    version: int = 1
    drafted_by: str = ""
    photos: list[PhotoLabel]

    def drafts(self) -> list[str]:
        return [p.id for p in self.photos if p.label_status == "draft"]


def load_labels(path: Path) -> tuple[LabelSet, Path]:
    """라벨 파일을 읽어 검증한다. 사진 기준 디렉터리도 함께 돌려준다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = LabelSet.model_validate(data)
    ids = [p.id for p in labels.photos]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate photo id in labels")
    return labels, path.parent
