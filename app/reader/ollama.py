"""Ollama 로컬 판독기 (GTX 1660 Super 후보: Qwen 2.5 VL, Gemma 3).

사진이 PC 밖으로 나가지 않는다. 서버가 안 떠 있거나 모델이 없으면 ReaderUnavailable.
"""

import base64
import json
import time

import httpx

from app.reader.base import (
    CellReading,
    CellReadings,
    Layout,
    LayoutProposal,
    Purpose,
    ReaderError,
    ReaderUnavailable,
    normalize_cells,
)
from app.reader.prompts import CELLS_PROMPT, LAYOUT_PROMPT, cells_schema, layout_schema


class OllamaReader:
    provider = "ollama"

    def __init__(self, base_url: str, model: str, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)
        try:
            tags = self._client.get("/api/tags", timeout=3.0).raise_for_status().json()
        except httpx.HTTPError as e:
            raise ReaderUnavailable(f"ollama 서버에 연결 못 함 ({self.base_url})") from e
        names = {m.get("name") for m in tags.get("models", [])}
        if model not in names and f"{model}:latest" not in names:
            raise ReaderUnavailable(f"ollama 모델 없음: {model} (ollama pull {model})")

    def _ask(self, image: bytes, prompt: str, schema: dict) -> tuple[dict, str, int]:
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.standard_b64encode(image).decode("ascii")],
                }
            ],
        }
        started = time.perf_counter()
        try:
            res = self._client.post("/api/chat", json=body).raise_for_status().json()
        except httpx.HTTPError as e:
            raise ReaderError(f"ollama error: {e}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = res.get("message", {}).get("content", "")
        try:
            return json.loads(raw), raw, latency_ms
        except json.JSONDecodeError as e:
            raise ReaderError("ollama returned invalid json") from e

    def detect_layout(self, image: bytes) -> LayoutProposal:
        data, raw, latency_ms = self._ask(image, LAYOUT_PROMPT, layout_schema())
        try:
            layout = Layout(
                rows=data["rows"],
                cols=data["cols"],
                row_labels=data.get("row_labels", []),
                col_labels=data.get("col_labels", []),
            )
        except (KeyError, ValueError) as e:
            raise ReaderError(f"invalid layout from ollama: {e}") from e
        return LayoutProposal(
            provider=self.provider,
            model=self.model,
            is_mock=False,
            latency_ms=latency_ms,
            raw_response=raw,
            layout=layout,
            suggested_time_of_day=data.get("suggested_time_of_day", []),
            uncertain=bool(data.get("uncertain", True)),
        )

    def read_cells(self, image: bytes, layout: Layout, purpose: Purpose = "dose") -> CellReadings:
        prompt = CELLS_PROMPT.format(
            rows=layout.rows,
            cols=layout.cols,
            row_labels=", ".join(layout.row_labels) or "(없음)",
            col_labels=", ".join(layout.col_labels) or "(없음)",
            cell_ids=", ".join(layout.cell_ids()),
        )
        data, raw, latency_ms = self._ask(image, prompt, cells_schema(layout.cell_ids()))
        cells = []
        for c in data.get("cells", []):
            try:
                cells.append(
                    CellReading(
                        cell_id=c["cell_id"],
                        state=c["state"],
                        confidence=min(max(float(c.get("confidence", 0)), 0.0), 1.0),
                    )
                )
            except (KeyError, ValueError):
                continue  # 형식이 틀린 칸은 normalize_cells 에서 unclear 로 채워진다
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=False,
            latency_ms=latency_ms,
            raw_response=raw,
            cells=normalize_cells(layout, cells),
        )
