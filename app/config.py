"""환경 변수 설정. 값은 `.env` 또는 환경 변수에서 읽는다 (`.env.example` 참고)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    timezone: str = "Asia/Seoul"
    database_url: str = "postgresql+psycopg://woowang:woowang@localhost:5432/woowang"
    photo_dir: str = "./data/photos"
    # 사진 서명 URL용 HMAC 비밀값. 운영에서는 반드시 바꾼다
    photo_url_secret: str = "change-me"

    # 판독기 선택 (backend.md 8장). "mock" | "claude" | "ollama"
    reader_layout: str = "mock"
    reader_cells: str = "mock"

    anthropic_api_key: str | None = None
    claude_model: str = "claude-haiku-5-5"

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"

    reader_timeout_s: float = 60.0
    daily_reading_limit: int = 30

    # 재질문·인계 시간값. 앱은 이 값을 GET /me 로 내려받는다 (R-007 C5)
    reask_offsets_min: list[int] = Field(default_factory=lambda: [10, 20])
    no_response_handoff_min: int = 30
    retake_wait_min: int = 15

    @property
    def reader_is_mock(self) -> bool:
        return "mock" in (self.reader_layout, self.reader_cells)


@lru_cache
def get_settings() -> Settings:
    return Settings()
