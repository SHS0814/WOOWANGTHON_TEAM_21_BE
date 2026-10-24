"""정확도 검증용 라벨 형식.

라벨 파일(JSON) 하나에 사진 여러 장을 적는다. 사진 경로는 라벨 파일 기준 상대 경로다.

{
  "version": 1,
  "photos": [
    {
      "id": "home-0001",
      "file": "photos/home-0001.jpg",
      "pillbox": {                       # 사진 속 약통 구조 (app.reader.base.Layout 과 같은 형식)
        "name": "다이소 2x7",
        "layout": {"rows": 2, "cols": 7, "row_labels": ["아침", "저녁"],
                   "col_labels": ["월", "화", "수", "목", "금", "토", "일"]}
      },
      "cells": {"r0c0": "empty", "r0c1": "full", ...},   # 칸별 정답. empty | full. 모든 칸 필수
      "target_cell_id": "r0c1",          # (선택) 이번 복약 칸. 분석 때 따로 집계한다
      "conditions": {"lighting": "indoor_dim", "angle": "top", "lid": "open"},  # (선택) 촬영 조건
      "notes": ""
    }
  ]
}
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.reader.base import Layout

Truth = Literal["empty", "full"]


class PillboxLabel(BaseModel):
    name: str = ""
    layout: Layout


class PhotoLabel(BaseModel):
    id: str
    file: str
    pillbox: PillboxLabel
    cells: dict[str, Truth]
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
        return self


class LabelSet(BaseModel):
    version: int = 1
    photos: list[PhotoLabel]


def load_labels(path: Path) -> tuple[LabelSet, Path]:
    """라벨 파일을 읽어 검증한다. 사진 기준 디렉터리도 함께 돌려준다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = LabelSet.model_validate(data)
    ids = [p.id for p in labels.photos]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate photo id in labels")
    return labels, path.parent
