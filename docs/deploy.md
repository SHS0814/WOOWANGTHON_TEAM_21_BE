# 배포 안내 (사용자 PC 상시 가동)

서버는 사용자 PC에서 Docker Compose로 돌리고, 이미 있는 Cloudflare Tunnel로 공개한다 (backend.md 1·12장).
이 문서는 명령과 확인 절차만 적는다. 실제 터널 설정과 배포는 사용자가 한다.

명령은 모두 **저장소 루트**에서 실행한다. Windows는 PowerShell 또는 WSL 터미널, Linux·macOS는 일반 터미널 기준이다.

## 0. 준비물

- Docker Desktop (Windows·macOS) 또는 Docker Engine + Compose 플러그인 (Linux)
- 이미 설정된 Cloudflare Tunnel과 `cloudflared`
- (선택) 로컬 판독 모델을 쓸 때만 NVIDIA 드라이버 + NVIDIA Container Toolkit

## 1. 설정 파일 (.env)

```bash
cp .env.example .env
```

`.env` 는 **절대 커밋하지 않는다** (`.gitignore`에 있음). 바꿔야 하는 값:

| 변수 | 값 |
|---|---|
| `PUBLIC_BASE_URL` | 터널로 공개할 주소. 예: `https://woowang.example.com` (끝에 `/` 없음). 사진 URL에 쓰인다 |
| `PHOTO_URL_SECRET` | 긴 난수. 예: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD`, `DATABASE_URL` | 같은 비밀번호로 둘 다 바꾼다. `DATABASE_URL=postgresql+psycopg://woowang:<비밀번호>@db:5432/woowang` |
| `DEMO_MODE` | 시연 영상·심사 기간에만 `true`. 시연 트리거와 데모 가족 생성이 열린다 |
| `ENV` | 운영은 `prod` (기본 `dev`). `prod`에서는 `CORS_ORIGINS`에 `*`를 쓸 수 없다 |
| `CORS_ORIGINS` | 웹으로 앱을 열 때만 필요. 웹 앱 주소를 쉼표로 (예: `https://app.woowang.example`). 기본은 개발용 localhost. 휴대폰 앱(Expo 네이티브)은 CORS와 무관하다 |
| `EXPO_PUSH_ENABLED` | 보호자 푸시를 실제로 보낼 때 `true`. `EXPO_ACCESS_TOKEN`은 Expo 계정에서 푸시 보안을 켰을 때만 |
| `READER_LAYOUT`, `READER_CELLS` | 기본 `mock`(앱에 "모의 판독" 표시). 모델이 정해지면 `claude` 등으로 바꾸고 `ANTHROPIC_API_KEY`를 채운다 |

> `POSTGRES_PASSWORD`는 DB를 **처음** 만들 때만 적용된다. 이미 `./data/postgres`가 있으면 바꿔도 반영되지 않는다.

## 2. 실행

```bash
docker compose up -d --build
docker compose ps                     # api, db 가 running (db 는 healthy)
curl -s http://127.0.0.1:8000/health  # {"status":"ok", ...}
```

- `api`는 `127.0.0.1:8000`에만 열린다 (`.env`의 `API_PORT`로 바꿀 수 있다. 바꾸면 터널 URL도 같은 포트로). 외부 공개는 터널로만 한다
- 데이터는 `./data/postgres`(DB), `./data/photos`(사진)에 남는다. 컨테이너를 지워도 유지된다
- 코드를 갱신하면: `git pull && docker compose up -d --build`
- 로그: `docker compose logs -f api`

로컬 판독 모델(선택): `docker compose --profile ollama up -d` 후 `docker compose exec ollama ollama pull qwen2.5vl:7b`. `.env`의 `OLLAMA_URL`을 `http://ollama:11434`로 바꾼다.

## 3. Cloudflare Tunnel 연결

기존 터널에 공개 호스트 이름 하나를 추가해서 `http://localhost:8000`으로 보낸다.

**대시보드로 관리하는 터널이면:** Zero Trust → Networks → Tunnels → 해당 터널 → Public Hostname 추가
- Subdomain/Domain: 예) `woowang.example.com`
- Service: `HTTP`, URL: `localhost:8000`

**설정 파일(config.yml)로 관리하는 터널이면:** `ingress`에 한 줄 추가하고 `cloudflared`를 재시작한다.

```yaml
ingress:
  - hostname: woowang.example.com
    service: http://localhost:8000
  - service: http_status:404   # 기존 마지막 줄 유지
```

```bash
cloudflared tunnel ingress validate      # 설정 검사
cloudflared tunnel route dns <터널 이름> woowang.example.com   # DNS 레코드가 없을 때만
```

확인:

```bash
curl -s https://woowang.example.com/health
```

- `status: ok`가 나와야 한다. `reader.is_mock`이 `true`면 앱에 "모의 판독"이 표시되는 상태다
- 사진 URL이 `PUBLIC_BASE_URL`로 시작하는지 확인: 보호자 토큰으로 `GET /api/v1/pillboxes/current` 의 `last_refill.baseline_reading.photo.url`
- `cloudflared`가 PC 시작 시 자동 실행되도록 서비스로 설치되어 있는지 확인한다 (`cloudflared service install`)

## 4. 재부팅 뒤 자동 복구

목표: PC를 껐다 켜도 사람이 아무것도 하지 않고 `/health`가 다시 응답한다.

1. **Docker 자동 시작**
   - Windows·macOS: Docker Desktop → Settings → General → "Start Docker Desktop when you sign in" 켜기. Windows는 사용자 자동 로그인이 필요할 수 있다
   - Linux: `sudo systemctl enable docker`
2. **컨테이너 자동 재시작:** `docker-compose.yml`의 모든 서비스가 `restart: always`라 Docker가 뜨면 같이 뜬다. `db`가 준비된 뒤 `api`가 시작된다 (healthcheck)
3. **cloudflared 자동 시작:** 서비스로 설치 (`cloudflared service install`)
4. **절전 끄기:** Windows 전원 설정에서 "절전 모드: 안 함", macOS는 "잠자기 방지", Linux는 `systemctl mask sleep.target suspend.target`
   - **PC가 잠들면 1분 작업도 멈춘다.** 깨어날 때까지 무응답 인계·요약 푸시가 늦어진다 (QA Q-008: 맥이 23:41~23:57 잠들어 인계가 11분 늦음)
   - macOS에서 개발·QA·시연할 때는 서버를 띄운 터미널에서 `caffeinate -dims` 를 같이 켜 둔다 (전원 연결 권장. 배터리에서는 잠자기가 더 자주 든다)
5. **자동 업데이트 재부팅 주의:** 심사 기간(11/16~11/21)에는 OS 자동 업데이트로 인한 재부팅을 미뤄 둔다

**시험 (11/8 전에 한 번, R-007 권고):**

```bash
# 재부팅 전
docker compose ps
# PC 재부팅 → 로그인만 하고 아무것도 실행하지 않는다
# 5분 뒤 다른 기기(휴대폰 데이터망 등)에서
curl -s https://woowang.example.com/health
```

### 1분 작업이 늦을 때 (로그 확인)

서버는 1분 작업의 실제 간격을 재서 늦으면 경고를 남긴다 (`docker compose logs api | grep jobs`).

| 로그 | 뜻 | 할 일 |
|---|---|---|
| `jobs late: interval=986s (expected 60s), system was asleep ~926s` | PC가 잠들어 있었다 | 위 4번 절전 끄기 |
| `jobs late: interval=154s` (asleep 없음) | 서버가 바빴거나 DB가 느렸다 | 같은 시각의 `jobs slow`, 오류 로그 확인 |
| `jobs slow: run took 45.0s` | 한 회차가 너무 오래 걸렸다 | DB 상태, 가족 수 확인 |
| `jobs ok: 60 runs, max interval 61s, ...` | 1시간마다 남는 정상 요약 | 없음 |

macOS에서 잠들었던 시각은 `pmset -g log | grep -E " (Sleep|DarkWake|Wake) "` 로도 확인할 수 있다.

## 5. 외부 업타임 모니터

심사 기간에 서버가 꺼지면 바로 알 수 있게 외부에서 `/health`를 감시한다 (backend.md 12장).

- 무료 서비스 예: UptimeRobot, Better Stack, Cloudflare Health Checks 중 하나
- 설정: HTTP(S) 모니터, URL `https://woowang.example.com/health`, 주기 5분, 키워드 `"status":"ok"` 확인, 알림은 사용자 휴대폰(앱 푸시·이메일)
- 확인: `docker compose stop api`로 일부러 내려서 알림이 오는지 본 뒤 `docker compose start api`

`/health`는 토큰 없이 열리지만 개인정보를 담지 않는다 (상태, 버전, 시각, 판독기 종류만).

## 6. DB 백업 (pg_dump)

```bash
./scripts/backup_db.sh                    # backups/woowang-<시각>.dump 생성, 14일 지난 파일 삭제
BACKUP_PHOTOS=1 ./scripts/backup_db.sh    # 사진도 함께 묶기
```

- `backups/`는 커밋하지 않는다 (`.gitignore`). 민감 정보이므로 클라우드 동기화 폴더에 두지 않는다
- **매일 자동 실행:**
  - Linux·macOS (`crontab -e`): `30 3 * * * cd /경로/WOOWANGTHON_TEAM_00_BE && ./scripts/backup_db.sh >> backups/backup.log 2>&1`
  - Windows: 작업 스케줄러에서 매일 03:30에 `wsl -e bash -lc "cd /mnt/c/경로/WOOWANGTHON_TEAM_00_BE && ./scripts/backup_db.sh"` 실행 (WSL 사용 시). WSL이 없으면 PowerShell로 `docker compose exec -T db pg_dump -U woowang -d woowang --format=custom -f /tmp/w.dump; docker compose cp db:/tmp/w.dump backups\woowang.dump`
- **복구 확인 (한 번은 해 본다):**

```bash
docker compose exec -T db createdb -U woowang woowang_restore_test
docker compose exec -T db pg_restore -U woowang -d woowang_restore_test < backups/woowang-<시각>.dump
docker compose exec -T db psql -U woowang -d woowang_restore_test -c "select count(*) from families;"
docker compose exec -T db dropdb -U woowang woowang_restore_test
```

실제 복구는 `api`를 멈추고(`docker compose stop api`) `woowang` DB에 `pg_restore --clean`으로 되돌린 뒤 다시 시작한다.

## 7. 데모 가족 (시연 영상·심사)

`.env`에 `DEMO_MODE=true`를 두고 서버를 띄운 뒤:

```bash
docker compose exec api uv run --no-sync python -m app.seed_demo create
```

- 데모 가족, 보호자·어르신 기기, 2×7 약통, 일정(아침 08:00·저녁 19:00), 모두 채운 기준선을 만든다
- 보호자·어르신 토큰과 보호자 초대 코드는 **화면에만** 출력된다. 파일·README·커밋에 남기지 않는다
- 심사용 접근 정보는 **비공개 제출 폼에만** 적는다 (R-007 C4). 심사용 가족은 샘플 약통 사진만 쓴다
- 시연 중 "지금 약 시간"은 `POST /api/v1/dev/dose-events`로 연다 (앱의 시연 버튼). 그 건은 앱에 "시뮬레이션"으로 표시된다

**심사 종료(11/21) 뒤 반드시:**

```bash
docker compose exec api uv run --no-sync python -m app.seed_demo revoke   # 데모 가족 기기 토큰 전부 폐기
```

그다음 `.env`의 `DEMO_MODE=false`로 바꾸고 `docker compose up -d`.

## 8. 점검표

| 항목 | 확인 방법 |
|---|---|
| 서버 응답 | `curl https://<도메인>/health` 가 `ok` |
| 비밀값 | `git status`에 `.env`, `backups/`, `data/`가 나오지 않음 |
| 재부팅 복구 | 4장 시험 통과 (날짜 기록) |
| 업타임 모니터 | 일부러 내렸을 때 알림 수신 |
| 백업 | 오늘 날짜의 `backups/*.dump` 존재, 복구 확인 1회 |
| 데모 가족 | 시드 완료, 접근 정보는 제출 폼에만 |
| 모의 판독 표시 | `/health`의 `reader.is_mock`과 앱의 "모의 판독" 띠가 일치 |
