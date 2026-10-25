"""AI 판독 인터페이스 (backend.md 8장).

구현체: MockReader, ClaudeReader, OpenAIReader, OllamaReader. 어떤 구현체든 결과에 provider, model,
is_mock, 지연 시간, 원본 응답을 담는다. is_mock=True 결과는 앱에 "모의 결과"로 표시된다.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

CellState = Literal["empty", "full", "unclear"]
TimeOfDay = Literal["morning", "noon", "evening", "bedtime"]
# 판독 목적. baseline = 약통 등록·리필 기준선 (채운 직후 사진), dose = 매 복약
Purpose = Literal["dose", "baseline"]


class Cell(BaseModel):
    id: str
    row: int
    col: int


class Layout(BaseModel):
    """약통 구조. rows×cols 격자이며 cells 는 판독할 칸 목록이다 (backend.md 4.1)."""

    rows: int = Field(ge=1, le=7)
    cols: int = Field(ge=1, le=7)
    row_labels: list[str] = Field(default_factory=list)
    col_labels: list[str] = Field(default_factory=list)
    cells: list[Cell] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_cells(self) -> "Layout":
        if not self.cells:
            self.cells = [
                Cell(id=f"r{r}c{c}", row=r, col=c)
                for r in range(self.rows)
                for c in range(self.cols)
            ]
        for cell in self.cells:
            if not (0 <= cell.row < self.rows and 0 <= cell.col < self.cols):
                raise ValueError(f"cell {cell.id} is outside {self.rows}x{self.cols}")
        return self

    def cell_ids(self) -> list[str]:
        return [c.id for c in self.cells]


class ReaderMeta(BaseModel):
    provider: str
    model: str
    is_mock: bool
    latency_ms: int = 0
    raw_response: str | None = None
    # 토큰 사용량 (비용 계산용). 판독기가 알려주지 않으면 None
    input_tokens: int | None = None
    output_tokens: int | None = None


class LayoutProposal(ReaderMeta):
    layout: Layout
    suggested_time_of_day: list[TimeOfDay | None] = Field(default_factory=list)
    uncertain: bool = False


class CellReading(BaseModel):
    cell_id: str
    state: CellState
    confidence: float = Field(ge=0.0, le=1.0)
    # 사진 안 칸 위치 [x, y, w, h] (0~1). 모델이 못 주면 None
    bbox: tuple[float, float, float, float] | None = None


class CellReadings(ReaderMeta):
    cells: list[CellReading]

    def by_id(self) -> dict[str, CellReading]:
        return {c.cell_id: c for c in self.cells}


class ReaderUnavailable(RuntimeError):
    """키나 서버가 없어서 판독기를 쓸 수 없음. 하네스는 이 판독기를 건너뛴다."""


class ReaderError(RuntimeError):
    """판독 호출 실패 (네트워크, 응답 형식 오류 등). 서버는 이를 "애매"로 처리한다."""


class Reader(Protocol):
    provider: str
    model: str

    def detect_layout(self, image: bytes) -> LayoutProposal: ...

    def read_cells(
        self, image: bytes, layout: Layout, purpose: Purpose = "dose"
    ) -> CellReadings: ...


def normalize_cells(layout: Layout, cells: list[CellReading]) -> list[CellReading]:
    """모델 응답을 layout 의 칸 순서로 맞춘다. 빠진 칸은 unclear, 모르는 칸은 버린다."""
    got = {c.cell_id: c for c in cells}
    return [
        got.get(cid) or CellReading(cell_id=cid, state="unclear", confidence=0.0)
        for cid in layout.cell_ids()
    ]


def sniff_media_type(image: bytes) -> str:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    if image[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    raise ReaderError("unsupported image format (png/jpeg/webp/gif only)")
