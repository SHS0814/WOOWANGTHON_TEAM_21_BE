"""Claude 판독기. structured output(JSON 스키마)으로 응답 형식을 강제한다.

사진이 Anthropic으로 전송된다 (backend.md 11장). 키가 없으면 ReaderUnavailable.
"""

import base64
import json
import time

from app.reader.base import (
    CellReading,
    CellReadings,
    Layout,
    LayoutProposal,
    ReaderError,
    ReaderUnavailable,
    normalize_cells,
    sniff_media_type,
)
from app.reader.prompts import CELLS_PROMPT, LAYOUT_PROMPT, cells_schema, layout_schema


class ClaudeReader:
    provider = "claude"

    def __init__(self, api_key: str | None, model: str, timeout_s: float = 60.0) -> None:
        if not api_key:
            raise ReaderUnavailable("ANTHROPIC_API_KEY 가 없음")
        try:
            import anthropic
        except ImportError as e:
            raise ReaderUnavailable("anthropic 패키지 없음 (uv sync --extra claude)") from e
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)
        self.model = model

    def _ask(self, image: bytes, prompt: str, schema: dict) -> tuple[dict, str, int]:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": sniff_media_type(image),
                    "data": base64.standard_b64encode(image).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ]
        started = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.APIError as e:
            raise ReaderError(f"claude api error: {e}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ReaderError(f"claude stop_reason={response.stop_reason}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ReaderError("claude returned no text block")
        try:
            return json.loads(text), text, latency_ms
        except json.JSONDecodeError as e:
            raise ReaderError("claude returned invalid json") from e

    def detect_layout(self, image: bytes) -> LayoutProposal:
        data, raw, latency_ms = self._ask(image, LAYOUT_PROMPT, layout_schema())
        try:
            layout = Layout(
                rows=data["rows"],
                cols=data["cols"],
                row_labels=data["row_labels"],
                col_labels=data["col_labels"],
            )
        except ValueError as e:
            raise ReaderError(f"invalid layout from claude: {e}") from e
        return LayoutProposal(
            provider=self.provider,
            model=self.model,
            is_mock=False,
            latency_ms=latency_ms,
            raw_response=raw,
            layout=layout,
            suggested_time_of_day=data["suggested_time_of_day"],
            uncertain=data["uncertain"],
        )

    def read_cells(self, image: bytes, layout: Layout) -> CellReadings:
        prompt = CELLS_PROMPT.format(
            rows=layout.rows,
            cols=layout.cols,
            row_labels=", ".join(layout.row_labels) or "(없음)",
            col_labels=", ".join(layout.col_labels) or "(없음)",
            cell_ids=", ".join(layout.cell_ids()),
        )
        data, raw, latency_ms = self._ask(image, prompt, cells_schema(layout.cell_ids()))
        cells = [
            CellReading(
                cell_id=c["cell_id"],
                state=c["state"],
                confidence=min(max(float(c["confidence"]), 0.0), 1.0),
            )
            for c in data["cells"]
        ]
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=False,
            latency_ms=latency_ms,
            raw_response=raw,
            cells=normalize_cells(layout, cells),
        )
