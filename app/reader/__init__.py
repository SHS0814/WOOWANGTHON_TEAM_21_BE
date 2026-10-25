"""판독기 선택. spec 형식: "mock", "claude[:<model>]", "openai[:<model>]", "ollama[:<model>]"."""

import logging

from app.config import Settings
from app.reader.base import (
    CellReading,
    CellReadings,
    Layout,
    LayoutProposal,
    Reader,
    ReaderError,
    ReaderUnavailable,
)
from app.reader.mock import MockReader

__all__ = [
    "CellReading",
    "CellReadings",
    "Layout",
    "LayoutProposal",
    "MockReader",
    "Reader",
    "ReaderError",
    "ReaderUnavailable",
    "build_reader",
    "build_server_reader",
]

log = logging.getLogger(__name__)


def build_reader(spec: str, settings: Settings) -> Reader:
    kind, _, model = spec.partition(":")
    if kind == "mock":
        return MockReader()
    if kind == "claude":
        from app.reader.claude import ClaudeReader

        return ClaudeReader(
            settings.anthropic_api_key, model or settings.claude_model, settings.reader_timeout_s
        )
    if kind == "openai":
        from app.reader.openai import OpenAIReader

        return OpenAIReader(
            settings.openai_api_key,
            model or settings.openai_model,
            settings.reader_timeout_s,
            reasoning_effort=settings.openai_reasoning_effort,
        )
    if kind == "ollama":
        from app.reader.ollama import OllamaReader

        return OllamaReader(
            settings.ollama_url, model or settings.ollama_model, settings.reader_timeout_s
        )
    raise ValueError(f"unknown reader spec: {spec!r}")


class UnavailableReader:
    """키·서버가 없어 만들 수 없는 판독기 자리. 모든 판독이 ReaderError 로 실패한다.

    서버는 이 실패를 판독 애매로 처리한다 (재촬영 → 보호자 인계, 원칙 5). 서버가 시작조차 못 하면
    어르신이 사진을 올릴 수도 없으므로, 시작은 하고 판독만 실패시킨다.
    """

    def __init__(self, provider: str, model: str, reason: str) -> None:
        self.provider = provider
        self.model = model
        self.reason = reason

    def detect_layout(self, image: bytes) -> LayoutProposal:
        raise ReaderError(f"reader unavailable: {self.reason}")

    def read_cells(self, image: bytes, layout: Layout, purpose: str = "dose") -> CellReadings:
        raise ReaderError(f"reader unavailable: {self.reason}")


def build_server_reader(spec: str, settings: Settings) -> Reader:
    """서버용. 판독기를 만들 수 없으면 오류를 남기고 항상 실패하는 판독기를 돌려준다."""
    try:
        return build_reader(spec, settings)
    except ReaderUnavailable as e:
        log.error("판독기 %r 를 쓸 수 없음: %s. 모든 판독이 애매로 처리된다", spec, e)
        kind, _, model = spec.partition(":")
        return UnavailableReader(kind, model or "unavailable", str(e))
