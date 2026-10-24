# WOOWANGTHON_TEAM_00_BE

치매 어르신 복약 확인 앱의 서버 + AI 약통 판독 모듈. 2026 AI 우왕톤 출품작.
사용자 PC에서 FastAPI 서버를 돌리고 Cloudflare Tunnel로 공개한다.

> **현재 상태:** 서버 뼈대(`/health`), AI 판독 인터페이스, 판독 정확도 검증 하네스만 있다.
> API 엔드포인트는 API 계약서 검토 승인 뒤 구현한다.
> 기본 판독기는 **모의 판독기(MockReader)** 이며, 그 결과는 `is_mock=true` 로 표시된다. 실제 AI 결과가 아니다.

## 구성

```
app/
  main.py            FastAPI 앱 (/health)
  config.py          환경 변수 설정 (.env)
  reader/            AI 판독 인터페이스
    base.py          Reader 프로토콜, Layout, CellReadings
    mock.py          MockReader (모의, is_mock=true)
    claude.py        ClaudeReader (Anthropic API, structured output)
    ollama.py        OllamaReader (로컬 모델)
    prompts.py       판독 프롬프트 초안
harness/             판독 정확도 검증
  labels.py          라벨 형식
  run.py             여러 판독기 비교 → 표
  make_samples.py    합성 샘플 생성기
  samples/           합성 샘플 이미지와 라벨 (실제 사진 아님)
tests/
```

## 실행 (로컬)

[uv](https://docs.astral.sh/uv/) 가 필요하다.

```bash
cp .env.example .env          # 필요한 값만 채운다. .env 는 커밋하지 않는다
uv sync --extra claude        # Claude 판독기를 안 쓰면 --extra 생략
uv run uvicorn app.main:create_app --factory --reload
curl http://localhost:8000/health
```

테스트와 린트:

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## 실행 (Docker Compose, 초안)

```bash
cp .env.example .env
docker compose up -d --build            # api + db
docker compose --profile ollama up -d   # 로컬 판독 모델까지 (선택, NVIDIA GPU)
```

`api` 는 `127.0.0.1:8000` 에만 열린다. 외부 공개는 Cloudflare Tunnel 이 이 포트로 연결해서 한다.

## 판독 정확도 검증

같은 라벨 데이터로 여러 판독기를 돌려 칸 단위 정확도, 애매 비율, 지연을 표로 낸다.

```bash
# 합성 샘플 + 모의 판독기로 동작 확인
uv run python -m harness.run --labels harness/samples/labels.json --readers mock

# 실제 사진으로 비교 (키·서버가 없는 판독기는 건너뜀)
uv run python -m harness.run --labels harness/data/labels.json \
  --readers mock,claude:claude-haiku-5-5,claude:claude-sonnet-5-5,ollama:qwen2.5vl:7b \
  --layout --thresholds 0.7,0.8,0.9 --out-dir harness/results
```

- 라벨 형식은 `harness/labels.py` 의 docstring 참고. 사진마다 약통 구조(`layout`)와 칸별 정답(`empty`/`full`)을 적는다
- **실사진은 `harness/data/` 에 둔다. 이 폴더와 `harness/results/` 는 커밋하지 않는다** (개인정보)
- 표의 "약 있는데 빈 칸으로 봄"은 복용으로 잘못 기록될 수 있는 위험 오류다
- `--thresholds` 는 신뢰도가 기준보다 낮은 판독을 애매로 바꿔서 다시 센다. "정상" 판정 기준을 정하는 데 쓴다

## 외부 기술

| 기술 | 용도 |
|---|---|
| FastAPI, Pydantic | 웹 서버 |
| Anthropic Claude API | 약통 판독 (선택, 사진이 Anthropic 으로 전송됨) |
| Ollama + 오픈소스 VLM | 약통 판독 (선택, 로컬) |
| PostgreSQL | DB (예정) |

## 심사용 접근 정보

이 저장소는 Public 이다. 심사용 접근 정보(토큰 등)는 README 에 적지 않고 비공개 제출 폼에만 적는다.
