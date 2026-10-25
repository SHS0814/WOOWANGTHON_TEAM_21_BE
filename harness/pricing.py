"""모델별 토큰 가격 (USD / 100만 토큰, 표준 요금). 하네스의 "사진 1장당 비용"에 쓴다.

확인한 날짜와 출처를 함께 적는다. 비교 실행(2단계) 전에 다시 확인하고 고친다.
- Anthropic: Claude API 문서 모델 표 (2026-10-25 확인). Haiku 5.5 는 입력 10만 토큰 이하 요금
- OpenAI: https://developers.openai.com/api/docs/pricing 와 모델별 페이지 (2026-10-25 확인).
  gpt-6-luna·gpt-6.1-sol·gpt-5.4-mini 는 이미지 입력·Responses API·structured outputs 지원 확인
로컬(ollama)과 모의(mock) 판독기는 API 비용이 0 이다. 표에 없는 모델은 비용을 "-" 로 낸다.
"""

PRICES_AS_OF = "2026-10-25"

# model -> (입력 $/MTok, 출력 $/MTok)
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.00, 10.00),
}

FREE_PROVIDERS = {"mock", "ollama"}


def call_cost(
    provider: str, model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """호출 1번의 비용(USD). 알 수 없으면 None."""
    if provider in FREE_PROVIDERS:
        return 0.0
    price = PRICES.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
