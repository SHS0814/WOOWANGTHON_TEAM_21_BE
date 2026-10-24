"""판독기 선택. spec 형식: "mock", "claude", "claude:<model>", "ollama", "ollama:<model>"."""

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
]


def build_reader(spec: str, settings: Settings) -> Reader:
    kind, _, model = spec.partition(":")
    if kind == "mock":
        return MockReader()
    if kind == "claude":
        from app.reader.claude import ClaudeReader

        return ClaudeReader(
            settings.anthropic_api_key, model or settings.claude_model, settings.reader_timeout_s
        )
    if kind == "ollama":
        from app.reader.ollama import OllamaReader

        return OllamaReader(
            settings.ollama_url, model or settings.ollama_model, settings.reader_timeout_s
        )
    raise ValueError(f"unknown reader spec: {spec!r}")
