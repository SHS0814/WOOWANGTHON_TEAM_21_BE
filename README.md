# WOOWANGTHON_TEAM_00_BE

치매 어르신 복약 확인 앱의 서버 + AI 약통 판독 모듈. 2026 AI 우왕톤 출품작.
사용자 PC에서 FastAPI 서버를 돌리고 Cloudflare Tunnel로 공개한다.

> **현재 상태:** API 계약서(DOCS 저장소 `docs/design/api.md`)의 엔드포인트를 모두 구현했다.
> 기본 판독기는 **모의 판독기(MockReader)** 이며, 그 결과는 `is_mock=true` 로 내려가고 앱에 "모의 판독"으로 표시된다. 실제 AI 결과가 아니다.

## 구성

```
app/
  main.py            FastAPI 앱 (create_app 팩토리)
  routers/           API 경로 (api.md 3장)
  services/          분류·기대 상태·복약 흐름·요약
  jobs.py            1분 주기 작업
  models.py          DB 테이블
  config.py          환경 변수 설정 (.env)
  reader/            AI 판독 인터페이스
    base.py          Reader 프로토콜, Layout, CellReadings
    mock.py          MockReader (모의, is_mock=true)
    claude.py        ClaudeReader (Anthropic API, structured output)
    openai.py        OpenAIReader (GPT. 실제 판독 기본 후보, R-037)
    ollama.py        OllamaReader (로컬 모델)
    prompts.py       판독 프롬프트 초안
harness/             판독 정확도 검증
  labels.py          라벨 형식
  run.py             여러 판독기 비교 → 표
  make_samples.py    합성 샘플 생성기
  review_sheet.py    라벨 확인 시트(HTML)
  pricing.py         모델별 토큰 가격
  README.md          하네스 사용법 (로컬 모델 실행 안내 포함)
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

## 주요 환경 변수

`.env.example` 참고. 자주 바꾸는 것:

| 변수 | 기본 | 뜻 |
|---|---|---|
| `READER_LAYOUT`, `READER_CELLS` | `mock` | 판독기 (`mock` / `claude[:모델]` / `openai[:모델]` / `ollama[:모델]`) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | (비움), `gpt-6-luna` | GPT 판독기. 키가 없으면 서버는 뜨고 모든 판독이 애매(재촬영 → 인계)로 처리된다 |
| `OPENAI_REASONING_EFFORT` | (비움) | GPT 추론 강도. 비우면 모델 기본값. 낮추면 빨라지지만 시험에서 '약 있는데 빈 칸' 쪽 불일치가 늘었다 |
| `PUBLIC_BASE_URL` | (비움) | 앱에 내려주는 사진 URL 앞부분 (Cloudflare Tunnel 주소) |
| `JOBS_ENABLED` | `true` | 1분마다 복약 건 열기·무응답 인계·요약 푸시·사진 정리 |
| `EXPO_PUSH_ENABLED` | `false` | 보호자 푸시 실제 발송. 끄면 로그만 남긴다 |
| `DEMO_MODE` | `false` | 시연 트리거 `POST /api/v1/dev/dose-events` (데모 가족만). 심사용 서버에서만 켠다 |

API 문서는 서버를 띄운 뒤 `/docs` 에서도 볼 수 있다.

## 실행 (Docker Compose)

상시 가동, Cloudflare Tunnel 연결, 재부팅 복구, 업타임 모니터, 백업, 데모 가족은 [docs/deploy.md](docs/deploy.md) 참고.

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
uv run python -m harness.run --labels harness/data/real/labels.json \
  --readers mock,claude:claude-haiku-5-5,claude:claude-sonnet-5-5,ollama:qwen2.5vl:7b \
  --layout --thresholds 0.7,0.8,0.9 --out-dir harness/results
```

- 라벨 형식은 `harness/labels.py` 의 docstring 참고. 사진마다 약통 구조(`layout`)와 칸별 정답(`empty`/`full`/`not_visible`)을 적는다
- 자세한 사용법, 라벨 확인 시트, 로컬 모델(PC) 실행 안내: `harness/README.md`
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
