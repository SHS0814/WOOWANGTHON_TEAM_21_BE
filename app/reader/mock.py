"""모의 판독기. 실제 AI가 아니다. 결과는 항상 is_mock=True 이다 (헌장 6장).

사진 바이트의 해시로 결과를 정하므로 같은 사진이면 항상 같은 결과가 나온다.
"""

import hashlib
import json
import random
from typing import Literal

from app.reader.base import (
    CellReading,
    CellReadings,
    Layout,
    LayoutProposal,
    Purpose,
)

DEFAULT_LAYOUT = Layout(
    rows=2,
    cols=7,
    row_labels=["아침", "저녁"],
    col_labels=["월", "화", "수", "목", "금", "토", "일"],
)


class MockReader:
    provider = "mock"
    model = "mock-v1"

    def __init__(self, empty_ratio: float = 0.45, unclear_ratio: float = 0.1) -> None:
        self.empty_ratio = empty_ratio
        self.unclear_ratio = unclear_ratio

    def detect_layout(self, image: bytes) -> LayoutProposal:
        return LayoutProposal(
            provider=self.provider,
            model=self.model,
            is_mock=True,
            layout=DEFAULT_LAYOUT,
            suggested_time_of_day=["morning", "evening"],
            uncertain=False,
            raw_response=json.dumps({"mock": True}),
        )

    def read_cells(self, image: bytes, layout: Layout, purpose: Purpose = "dose") -> CellReadings:
        seed = hashlib.sha256(image).hexdigest()
        if purpose == "baseline":
            # 기준선은 "약을 채운 약통" 사진이다. 무작위로 빈 칸을 만들면 시연 중에
            # 엉뚱한 인계(refill_overdue 등)가 생기므로 모두 full 로 돌려준다 (R-015 C1)
            return CellReadings(
                provider=self.provider,
                model=self.model,
                is_mock=True,
                cells=[
                    CellReading(cell_id=cid, state="full", confidence=0.99)
                    for cid in layout.cell_ids()
                ],
                raw_response=json.dumps({"mock": True, "purpose": "baseline"}),
            )
        cells = []
        for cid in layout.cell_ids():
            rng = random.Random(f"{seed}:{cid}")
            r = rng.random()
            if r < self.unclear_ratio:
                state = "unclear"
            elif r < self.unclear_ratio + self.empty_ratio:
                state = "empty"
            else:
                state = "full"
            cells.append(
                CellReading(cell_id=cid, state=state, confidence=round(rng.uniform(0.55, 0.99), 2))
            )
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=True,
            cells=cells,
            raw_response=json.dumps({"mock": True, "seed": seed[:12]}),
        )


Scenario = Literal["normal", "unclear", "unchanged", "double_suspected"]


class ScenarioReader:
    """QA용 결정적 모의 판독 (R-018). 정해진 시나리오가 나오도록 칸 결과를 만든다.

    사진은 보지 않는다. 기대 상태(expected)를 기준으로 대상 칸과 다른 칸을 정한다.
    결과는 MockReader 와 같이 is_mock=True 다.
    """

    provider = MockReader.provider
    model = MockReader.model

    def __init__(self, scenario: Scenario, expected: dict[str, str], target_cell_id: str) -> None:
        self.scenario = scenario
        self.expected = expected
        self.target = target_cell_id

    def detect_layout(self, image: bytes) -> LayoutProposal:
        return MockReader().detect_layout(image)

    def read_cells(self, image: bytes, layout: Layout, purpose: Purpose = "dose") -> CellReadings:
        # 다른 칸은 기대대로 (비교 제외 칸은 차 있는 것으로)
        states = {
            cid: "empty" if self.expected.get(cid) == "empty" else "full"
            for cid in layout.cell_ids()
        }
        states[self.target] = {"unclear": "unclear", "unchanged": "full"}.get(
            self.scenario, "empty"
        )
        if self.scenario == "double_suspected":
            other = next(
                (
                    c
                    for c in layout.cell_ids()
                    if c != self.target and self.expected.get(c) == "full"
                ),
                None,
            )
            if other is not None:
                states[other] = "empty"
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=True,
            cells=[
                CellReading(cell_id=cid, state=states[cid], confidence=0.95)
                for cid in layout.cell_ids()
            ],
            raw_response=json.dumps({"mock": True, "scenario": self.scenario}),
        )
