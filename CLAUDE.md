# CLAUDE.md — WOOWANGTHON_TEAM_00_BE

Python 서버 (FastAPI 등) + AI 약통 판독 모듈. 사용자 PC에서 Cloudflare Tunnel로 공개한다.
치매 어르신 복약 확인 앱, 2026 AI 우왕톤 출품작. 1인 개발. 마감은 2026-11-15 18:00.

## 프로젝트 문서 (DOCS 저장소)

모든 기준 문서는 이 저장소가 아니라 DOCS 저장소에 있다: `/Users/decehnk/project/woowang/woowang`

먼저 읽을 것:
1. `/Users/decehnk/project/woowang/woowang/docs/PROJECT_CHARTER.md`: 큰 틀 (핵심 흐름, 기능 범위, 설계 원칙, 대회 제약)
2. `/Users/decehnk/project/woowang/woowang/docs/REVIEW_PROTOCOL.md`: 검토·승인 절차
3. `/Users/decehnk/project/woowang/woowang/docs/REVIEW_LOG.md`: 지금까지의 판정과 사용자 결정
4. 설계: `/Users/decehnk/project/woowang/woowang/docs/design/backend.md (R-007 조건 포함)`

## 규칙

- 이 프로젝트에는 **메인 에이전트(리뷰어)** 가 있다. 기능 추가, 설계 변경, API 변경, 기술 결정은 `/Users/decehnk/project/woowang/woowang/docs/reviews/requests/` 에 검토 요청을 올리고 승인받은 뒤 진행한다. DOCS 저장소의 다른 파일은 고치지 않는다
- 헌장 4장 "범위 밖" 기능은 구현하지 않는다
- 모킹이나 하드코딩된 AI 결과는 반드시 화면에 표시한다 (대회 규정)
- 앱은 의료 조언(추가 복용 지시, 용량 안내)을 하지 않는다
- **비밀값(API 키, 토큰, .env)은 절대 커밋하지 않는다.** 저장소는 나중에 Public으로 전환되고, 커밋 기록 전체가 공개된다. `.env.example` 만 커밋한다
- 기본 브랜치는 `main`. 작업은 브랜치에서 하고 main 병합 전 메인 에이전트 검토를 받는다
- **작업을 끝내거나, 막히거나, 검토 요청을 올리면 메인 에이전트 터미널에 직접 알린다.** 방법은 `/Users/decehnk/project/woowang/woowang/docs/REVIEW_PROTOCOL.md`의 "완료·검토 요청 알림" 절을 따른다. 사용자에게 대신 전해 달라고 하지 않는다
