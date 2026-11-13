"""판독 분류 (backend.md 5·6장, R-010 2번, R-014). DB 없이 판독 결과와 기대 상태만 보고 정한다.

분류 순서:
0. 비스듬한 사진: 판독기가 camera_angle=oblique 라고 답하면 칸 판독과 상관없이 애매 (다시 찍기)
1. 이미 쓴 칸 (R-014): 대상 칸이 이번 주기에 이미 비어 있어야 하는 칸인데 비어 보임 → 정상 금지
2. 이중 복용 의심: 기대상 차 있어야 할 다른 칸이 비어 보임
3. 애매: 대상 칸 unclear·신뢰도 부족, 또는 다른 칸 unclear
4. 칸 그대로: 대상 칸이 차 있음
5. 정상. 다른 칸 일부 불일치는 attention 으로만 남긴다 (R-010 2번)

리필 미기록 의심(5.4)은 분류와 따로 판단한다.
"""

from dataclasses import dataclass, field
from typing import Literal

from app.reader import CellReadings

Kind = Literal["normal", "unclear", "unchanged", "double_suspected", "already_used"]


@dataclass
class Classified:
    kind: Kind
    cells: list[dict]  # api.md CellResult
    refill_suspected: bool  # 대상 칸 외 모두 full, 그중 기대상 empty 가 1개 이상 (5.4)
    # 리필 "아니요" 때 ignored 로 바꿀 칸
    refill_full_cells: list[str] = field(default_factory=list)


def classify(
    result: CellReadings,
    expected: dict[str, str],
    target_cell_id: str,
    threshold: float,
) -> Classified:
    """expected: 이번 복약 건 직전의 칸별 기대 상태 (full | empty | ignored)."""
    by_id = result.by_id()
    target = by_id[target_cell_id]
    target_known_empty = expected.get(target_cell_id) == "empty"

    cells = []
    for c in result.cells:
        exp = "empty" if c.cell_id == target_cell_id else expected.get(c.cell_id, "ignored")
        attention = None
        if c.state == "unclear":
            attention = "unclear"
        elif exp == "full" and c.state == "empty":
            attention = "unexpected_empty"
        elif exp == "empty" and c.state == "full":
            attention = "unexpected_full"
        elif c.confidence < threshold:
            attention = "low_confidence"
        cells.append(
            {
                "cell_id": c.cell_id,
                "ai_state": c.state,
                "expected": exp,
                "attention": attention,
                "bbox": list(c.bbox) if c.bbox else None,
            }
        )

    others = [c for c in result.cells if c.cell_id != target_cell_id]
    refill_full = [
        c.cell_id for c in others if c.state == "full" and expected.get(c.cell_id) == "empty"
    ]
    refill_suspected = bool(others) and all(c.state == "full" for c in others) and bool(refill_full)

    def done(kind: Kind) -> Classified:
        return Classified(kind, cells, refill_suspected, refill_full)

    # 비스듬히 찍은 사진은 약이 뚜껑·칸막이에 가려 빈 칸처럼 보일 수 있다 (2026-11-13 B 약통 평가).
    # 칸 판독과 상관없이 애매로 돌려 다시 찍게 한다. 이중 복용 의심·정상 어느 쪽으로도 쓰지 않는다
    if result.camera_angle == "oblique":
        return done("unclear")
    if target_known_empty and target.state == "empty":
        return done("already_used")
    if any(c.state == "empty" and expected.get(c.cell_id) == "full" for c in others):
        return done("double_suspected")
    if target.state == "unclear" or any(c.state == "unclear" for c in others):
        return done("unclear")
    if target.state == "full":
        return done("unchanged")
    if target.confidence < threshold:
        return done("unclear")
    return done("normal")
