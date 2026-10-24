"""모의 판독기. 실제 AI가 아니다. 결과는 항상 is_mock=True 이다 (헌장 6장).

사진 바이트의 해시로 결과를 정하므로 같은 사진이면 항상 같은 결과가 나온다.
"""

import hashlib
import json
import random

from app.reader.base import (
    CellReading,
    CellReadings,
    Layout,
    LayoutProposal,
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

    def read_cells(self, image: bytes, layout: Layout) -> CellReadings:
        seed = hashlib.sha256(image).hexdigest()
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
