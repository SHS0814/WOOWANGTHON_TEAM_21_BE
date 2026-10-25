# 판독 정확도 하네스

같은 사진·같은 라벨·같은 프롬프트로 여러 판독기(Claude, GPT, 로컬 모델)를 돌려 칸 단위 지표를 표로 낸다.
계획: DOCS `docs/design/ai-model-eval-plan.md`

```
harness/
  labels.py        라벨 형식 (empty / full / not_visible, draft / confirmed)
  run.py           판독기 비교 → 표(md) + 칸별 상세(csv)
  pricing.py       모델별 토큰 가격 (사진 1장당 비용 계산)
  review_sheet.py  라벨 확인 시트(HTML) 생성
  make_samples.py  합성 샘플 생성기
  samples/         합성 샘플 (커밋됨, 실제 사진 아님)
  data/            실사진·라벨 (커밋 금지, .gitignore)
  results/         실행 결과 (커밋 금지, .gitignore)
```

**`harness/data/`와 `harness/results/`는 절대 커밋하지 않는다.** 약봉투에 이름·약국이 보일 수 있다.
사진·판독 결과·사진 속 글자를 검토 요청이나 다른 곳에 옮겨 적지 않는다. 사진은 파일 경로로만 가리킨다.

## 지표

| 열 | 뜻 |
|---|---|
| 칸 정확도 | 보이는 칸 중 정답 비율. 애매(unclear)도 틀린 것으로 셈 |
| 판단한 칸 정확도 | 애매를 뺀 칸 중 정답 비율 |
| 애매 비율 | 보이는 칸 중 애매로 답한 비율 |
| **약 있는데 빈 칸으로 봄** | 정답 full 인데 empty 로 답한 비율. 복용하지 않았는데 "드심"으로 기록될 수 있는 **가장 위험한 오류** |
| 빈 칸인데 약 있다고 봄 | 정답 empty 인데 full 로 답한 비율 |
| 안 보이는 칸 / 안 보이는 칸을 애매로 | 라벨이 `not_visible`인 칸 수, 그중 판독기가 애매로 돌린 비율. 나머지는 안 보이는 칸을 추측한 것 (원칙 5 위반) |
| 사진 1장당 비용 | 판독기가 알려준 토큰 수 × `pricing.py` 가격. 로컬·모의는 0 |
| 지연 p50/p95 | 판독 호출 1번 왕복 시간. p95 ≥ 20초면 비동기 판독 재검토 (R-010 9번) |

`--thresholds 0.7,0.9`는 신뢰도가 기준보다 낮은 판독을 애매로 바꿔 다시 센다. "정상" 판정 기준을 정하는 데 쓴다.

## 실사진 라벨 (사용자 확인 필요)

`harness/data/real/`에 실사진 50장(중복 4쌍 제외)과 라벨 초안이 있다.

- `photos/real-NNN.jpeg`, `manifest.json`(원본 파일명·해시·크기)
- `labels.draft.json`: AI 에이전트(Claude)가 사진을 **눈으로 보고** 만든 칸별 초안. 모든 사진이 `draft`다
- `review.html`: 확인 시트

**초안은 정답이 아니다.** Claude는 평가 대상 모델과 같은 계열이라, 확인 없이 쓰면 평가가 순환한다.
하네스는 `draft` 라벨이 하나라도 있으면 실행을 거부한다 (`--allow-draft`는 동작 확인용이고, 그 숫자는 결과로 쓰지 않는다).

확인 방법:
1. `harness/data/real/review.html`을 브라우저(Chrome·Edge 등)에서 파일로 연다 (더블클릭)
2. 사진마다 칸을 눌러 고친다 (약 있음 → 빔 → 안 보임). 주황 점선 칸은 초안 작성자가 확신하지 못한 칸이다
3. 다 봤으면 "이 사진 확인함"을 체크한다. 진행 상황은 브라우저에 임시 저장되고, 중간에 내보낸 파일은 "불러오기"로 이어서 할 수 있다
4. "labels.json 내보내기"로 받은 파일을 `harness/data/real/labels.json`으로 둔다
5. `uv run python -m harness.run --labels harness/data/real/labels.json --readers mock` 으로 형식을 확인한다 (draft 가 남아 있으면 몇 장인지 알려 준다)

시트를 다시 만들려면: `uv run python -m harness.review_sheet harness/data/real/labels.draft.json`

## API 모델 실행 (이 Mac 또는 PC)

키는 저장소 루트 `.env`에만 넣는다 (`.env.example` 참고). 대화·커밋에 넣지 않는다. 각 콘솔에서 비용 상한을 건다.

```bash
uv sync --extra claude          # anthropic SDK (OpenAI는 httpx 직접 호출이라 추가 설치 없음)
# .env: ANTHROPIC_API_KEY=..., OPENAI_API_KEY=...
uv run python -m harness.run --labels harness/data/real/labels.json \
  --readers openai:gpt-6-luna,openai:gpt-6.1-sol,claude:claude-haiku-5-5,claude:claude-sonnet-5-5 \
  --thresholds 0.7,0.8,0.9 --out-dir harness/results
```

모델 이름과 가격은 실행 직전에 다시 확인하고 `pricing.py`를 고친다. 추론 강도는 `OPENAI_REASONING_EFFORT`(비우면 모델 기본값)로 바꿀 수 있다.
호출별 지연·토큰·비용은 `--out-dir`의 `-calls.csv`에 남는다 (사진 종류별 비용 추정용).

## 로컬 모델 실행 (사용자 PC, GTX 1660 Super 6GB)

이 Mac에는 Ollama가 없다. 로컬 모델은 사용자 PC에서 돌린다. 사진이 PC 밖으로 나가지 않는다.

### 1. Ollama 설치

- Windows: https://ollama.com/download 에서 Windows 설치 파일을 받아 설치한다. 설치하면 백그라운드에서 서버가 뜬다
- Linux: `curl -fsSL https://ollama.com/install.sh | sh`

확인 (PowerShell 또는 터미널):
```bash
ollama --version
curl http://localhost:11434/api/tags
```

`docker-compose.yml`의 ollama 서비스(`ollama/ollama:0.5.4`)는 버전이 오래돼 새 비전 모델을 못 받을 수 있다. 비교 검증에는 위의 직접 설치를 쓴다.

### 2. 모델 받기

6GB VRAM에 들어가는 4비트 양자화 기본 태그 기준. 정확한 태그와 크기는 실행 전에 https://ollama.com/library 에서 다시 확인한다.

```bash
ollama pull qwen2.5vl:3b
ollama pull qwen2.5vl:7b      # 약 6GB. 일부가 CPU로 넘어가 느릴 수 있다
ollama pull gemma3:4b
ollama pull qwen3-vl:4b       # 있으면
ollama list
```

사진 한 장이 컨텍스트를 많이 쓰므로 Ollama 컨텍스트를 늘려 둔다 (기본값이 작으면 사진이 잘린다).
- Windows: 시스템 환경 변수에 `OLLAMA_CONTEXT_LENGTH=8192` 추가 후 Ollama를 트레이에서 종료했다 다시 연다
- Linux: `sudo systemctl edit ollama` → `[Service]` 아래 `Environment="OLLAMA_CONTEXT_LENGTH=8192"` → `sudo systemctl restart ollama`

### 3. 저장소와 데이터 준비

```bash
git clone <BE 저장소 주소> WOOWANGTHON_TEAM_00_BE
cd WOOWANGTHON_TEAM_00_BE
git checkout ai/model-eval
uv sync                       # uv 가 없으면: https://docs.astral.sh/uv/ 설치 안내
```

`harness/data/real/` 폴더(사진 + 확인한 `labels.json`)는 git에 없다. Mac에서 USB나 직접 복사로 PC의 같은 위치에 옮긴다.
클라우드 드라이브·메신저로 옮기지 않는다.

### 4. 실행

첫 호출은 모델을 GPU에 올리느라 오래 걸리므로 시간 제한을 늘린다.

```bash
# Linux·macOS
READER_TIMEOUT_S=300 uv run python -m harness.run --labels harness/data/real/labels.json \
  --readers ollama:qwen2.5vl:3b,ollama:qwen2.5vl:7b,ollama:gemma3:4b \
  --thresholds 0.7,0.8,0.9 --out-dir harness/results
```

```powershell
# Windows PowerShell
$env:READER_TIMEOUT_S = "300"
uv run python -m harness.run --labels harness/data/real/labels.json `
  --readers ollama:qwen2.5vl:3b,ollama:qwen2.5vl:7b,ollama:gemma3:4b `
  --thresholds 0.7,0.8,0.9 --out-dir harness/results
```

- 모델이 없으면 표 아래 "건너뛴 판독기"에 `ollama pull <모델>` 안내가 나온다
- 모델은 한 번에 하나씩 순서대로 돈다. 지연은 첫 장(모델 적재)이 튈 수 있으니 p50 위주로 본다
- 결과는 `harness/results/<시각>.md`(표)와 `-cells.csv`(칸별)에 생긴다. 이 폴더도 커밋하지 않는다. 표(md)만 Mac 쪽으로 옮겨 AI 에이전트에게 경로를 알려 준다
- `nvidia-smi`로 GPU 메모리를 보면서 돌리면 CPU로 넘어갔는지 알 수 있다

## 합성 샘플 (동작 확인)

```bash
uv run python -m harness.run --labels harness/samples/labels.json --readers mock
```
