"""OpenAI(GPT) 판독기. Responses API의 structured output(JSON 스키마, strict)으로 응답 형식을 강제한다.

ClaudeReader 와 같은 프롬프트·스키마를 쓴다. 사진이 OpenAI로 전송된다. 키가 없으면 ReaderUnavailable.
SDK 대신 httpx 로 직접 호출한다 (의존성 추가 없음, 테스트에서 전송을 바꿔 끼우기 쉬움).
"""

import base64
import copy
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
    sniff_media_type,
)
from app.reader.prompts import LAYOUT_PROMPT, cells_prompt, cells_schema, layout_schema

OPENAI_URL = "https://api.openai.com/v1"


def strict_schema(schema: dict) -> dict:
    """strict 모드는 enum 에도 type 이 있어야 한다. null 을 포함한 enum 에 type 을 붙인다."""
    schema = copy.deepcopy(schema)

    def fix(node):
        if isinstance(node, dict):
            if "enum" in node and "type" not in node:
                kinds = {"string" if isinstance(v, str) else "null" for v in node["enum"]}
                node["type"] = sorted(kinds) if len(kinds) > 1 else kinds.pop()
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    return schema


class OpenAIReader:
    provider = "openai"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if not api_key:
            raise ReaderUnavailable("OPENAI_API_KEY 가 없음")
        self.model = model
        # None 이면 모델 기본값. 지연을 줄이려면 "low" 등 (모델마다 허용 값이 다르다)
        self.reasoning_effort = reasoning_effort
        self._client = httpx.Client(
            base_url=OPENAI_URL,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def _post(self, body: dict) -> dict:
        """실패는 모두 ReaderError 로 바꾼다 (서버는 판독 애매로 처리, 원칙 5).

        연결 오류·속도 제한(429)·5xx 는 한 번만 다시 시도한다 (ClaudeReader 의 max_retries=1 과 맞춤).
        시간 초과는 다시 시도하지 않는다 (어르신이 결과를 기다리는 시간이 두 배가 되므로).
        크레딧 없음(insufficient_quota)도 다시 시도해도 같으므로 바로 실패한다.
        """
        for attempt in (1, 2):
            try:
                res = self._client.post("/responses", json=body)
            except httpx.TimeoutException as e:
                raise ReaderError("openai timeout") from e
            except httpx.HTTPError as e:
                if attempt == 1:
                    continue
                raise ReaderError(f"openai connection error: {type(e).__name__}") from e
            try:
                data = res.json()
            except ValueError:
                data = None
            error = data.get("error") if isinstance(data, dict) else None
            error = error if isinstance(error, dict) else {}
            retryable = (res.status_code == 429 and error.get("code") != "insufficient_quota") or (
                res.status_code >= 500
            )
            if retryable and attempt == 1:
                time.sleep(1.0)
                continue
            if res.status_code >= 400:
                code = f" {error['code']}" if error.get("code") else ""
                raise ReaderError(
                    f"openai api error {res.status_code}{code}: {error.get('message', '')}"
                )
            if not isinstance(data, dict):
                raise ReaderError("openai returned a non-json response")
            return data
        raise AssertionError("unreachable")

    def _ask(self, image: bytes, prompt: str, schema: dict, name: str) -> tuple[dict, str, dict]:
        data_url = f"data:{sniff_media_type(image)};base64," + base64.standard_b64encode(
            image
        ).decode("ascii")
        body = {
            "model": self.model,
            "max_output_tokens": 8192,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_url},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "schema": strict_schema(schema),
                    "strict": True,
                }
            },
        }
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        started = time.perf_counter()
        res = self._post(body)
        usage = res.get("usage") or {}
        meta = {
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
        if res.get("status") != "completed":
            reason = (res.get("incomplete_details") or {}).get("reason", res.get("status"))
            raise ReaderError(f"openai status={res.get('status')} ({reason})")
        text = None
        try:
            for item in res.get("output") or []:
                if item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if part.get("type") == "refusal":
                        raise ReaderError("openai refusal")
                    if part.get("type") == "output_text":
                        text = part.get("text")
        except AttributeError as e:
            raise ReaderError("openai returned an unexpected response shape") from e
        if not isinstance(text, str):
            raise ReaderError("openai returned no output_text")
        try:
            return json.loads(text), text, meta
        except json.JSONDecodeError as e:
            raise ReaderError("openai returned invalid json") from e

    def detect_layout(self, image: bytes) -> LayoutProposal:
        data, raw, meta = self._ask(image, LAYOUT_PROMPT, layout_schema(), "pillbox_layout")
        try:
            layout = Layout(
                rows=data["rows"],
                cols=data["cols"],
                row_labels=data["row_labels"],
                col_labels=data["col_labels"],
            )
            return LayoutProposal(
                provider=self.provider,
                model=self.model,
                is_mock=False,
                raw_response=raw,
                layout=layout,
                suggested_time_of_day=data["suggested_time_of_day"],
                uncertain=data["uncertain"],
                **meta,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ReaderError(f"invalid layout from openai: {e}") from e

    def read_cells(self, image: bytes, layout: Layout, purpose: Purpose = "dose") -> CellReadings:
        data, raw, meta = self._ask(
            image, cells_prompt(layout), cells_schema(layout.cell_ids()), "pillbox_cells"
        )
        try:
            cells = [
                CellReading(
                    cell_id=c["cell_id"],
                    state=c["state"],
                    confidence=min(max(float(c["confidence"]), 0.0), 1.0),
                )
                for c in data["cells"]
            ]
        except (KeyError, TypeError, ValueError) as e:
            raise ReaderError(f"invalid cells from openai: {e}") from e
        return CellReadings(
            provider=self.provider,
            model=self.model,
            is_mock=False,
            raw_response=raw,
            cells=normalize_cells(layout, cells),
            **meta,
        )
