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
    photo_url_ttl_s: int = 300
    photo_max_bytes: int = 10 * 1024 * 1024
    # 앱에 내려주는 URL의 앞부분 (예: https://woowang.example.com). 비우면 요청 주소를 쓴다
    public_base_url: str | None = None

    # 웹에서 앱을 열 때 허용할 출처 (쉼표로 구분). 기본은 개발용 localhost 만.
    # 운영에서는 실제 웹 앱 주소만 적는다. "*" 는 ENV=dev 에서만 허용한다
    cors_origins: str = (
        "http://localhost:8081,http://127.0.0.1:8081,http://localhost:19006,http://127.0.0.1:19006"
    )

    # 시연 트리거 (R-010 10번). true 이고 가족이 is_demo 일 때만 POST /dev/dose-events 동작
    demo_mode: bool = False

    elder_code_ttl_min: int = 10
    guardian_code_ttl_h: int = 24
    pair_fail_limit: int = 5
    pair_fail_window_min: int = 10
    consent_text_version: str = "v1"

    # 판독기 선택 (backend.md 8장). "mock" | "claude" | "openai" | "ollama"
    reader_layout: str = "mock"
    reader_cells: str = "mock"

    anthropic_api_key: str | None = None
    claude_model: str = "claude-haiku-5-5"

    # OpenAI 판독기 (정확도 비교용, T-AI1). 모델은 검증 시점에 다시 정한다
    openai_api_key: str | None = None
    openai_model: str = "gpt-6-luna"
    # 추론 강도. 비우면 모델 기본값 (gpt-6-luna 는 medium). 지연이 길면 low 로 낮춘다
    openai_reasoning_effort: str | None = None

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"

    reader_timeout_s: float = 60.0
    # "정상" 판정 신뢰도 기준. 임시값, 정확도 검증 후 다시 정한다 (backend.md 7.1)
    normal_confidence: float = 0.9
    daily_reading_limit: int = 30

    # 재질문·인계 시간값. 앱은 이 값을 GET /me 로 내려받는다 (R-007 C5)
    reask_offsets_min: list[int] = Field(default_factory=lambda: [10, 20])
    no_response_handoff_min: int = 30
    retake_wait_min: int = 15
    # 1분마다 복약 건 열기·인계 기한 확인·요약 푸시·정리 (backend.md 9장)
    jobs_enabled: bool = True
    # 보호자 푸시: "fcm" | "expo" | "recording"(보내지 않고 로그만). 비우면 예전처럼
    # EXPO_PUSH_ENABLED 로 정한다 (true → expo, 아니면 recording)
    push_provider: str | None = None
    # FCM 서비스 계정 키 JSON 경로 (R-039). 저장소 밖에 두고 커밋하지 않는다
    fcm_service_account_file: str | None = None
    # 앱이 만든 보호자 알림 채널 (FE src/notify/channels.ts 의 GUARDIAN_CHANNEL)
    fcm_android_channel_id: str = "guardian"
    expo_push_enabled: bool = False
    expo_access_token: str | None = None

    # 리필 안내 기준 = 하루 복용 횟수 × 이 값 (R-010 5번)
    refill_threshold_days: int = 2
    # 복약 시각 전 사진은 이 시간 전부터만 받음 (R-010 13번)
    early_photo_window_min: int = 60

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        if "*" in origins and self.env != "dev":
            raise ValueError("CORS_ORIGINS 에 * 는 ENV=dev 에서만 쓸 수 있다")
        return origins

    @property
    def effective_push_provider(self) -> str:
        if self.push_provider:
            return self.push_provider.strip().lower()
        return "expo" if self.expo_push_enabled else "recording"

    @property
    def ai_external_transfer(self) -> bool:
        kinds = {self.reader_layout.split(":")[0], self.reader_cells.split(":")[0]}
        return bool(kinds & {"claude", "openai"})

    @property
    def reader_is_mock(self) -> bool:
        return "mock" in (self.reader_layout.split(":")[0], self.reader_cells.split(":")[0])


@lru_cache
def get_settings() -> Settings:
    return Settings()
