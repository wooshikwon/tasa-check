# Phase 1 개발 방향서

## 목적

설계 문서(2025-01-15-tasa-check-design.md)는 **무엇을** 만들지 정의한다.
이 문서는 **어떻게** 만들지 — 개발 순서, agent 활용 전략, 평가 체계를 정의한다.

## Phase 1 범위

- /start: ConversationHandler 프로필 등록
- /check: 네이버 수집 → 언론사 필터 → 본문 추출 → Claude 분석 → 기사별 전송
- /setkey: API 키 변경
- 적응형 시간 윈도우 (기자 개인별)
- 의미 기반 중복 제거 (reported_articles)

---

## 개발 전략

**레이어별 순차 진행, 레이어 내 독립 모듈은 병렬 개발.**

각 레이어 완료 시 코드 리뷰를 거친 후 다음 레이어로 진행한다.
상위 레이어가 하위 레이어의 인터페이스에 의존하므로, 레이어 간 순서는 반드시 지킨다.

---

## 모듈 의존성

```
Layer 0 (기반)
  config.py ─────────────────────────────────┐
  storage/models.py ──→ storage/repository.py │
                                              │
Layer 1 (도구) ── 서로 독립, 병렬 가능 ────────┤
  tools/search.py      (Naver API)            │
  tools/scraper.py     (기사 본문 추출)        │
  filters/publisher.py (언론사 필터)           │
                                              │
Layer 2 (분석)                                │
  agents/check_agent.py ←── Layer 0 + 1 전체  │
                                              │
Layer 3 (봇)                                  │
  bot/formatters.py    (독립)                 │
  bot/conversation.py  (← repository)         │
  bot/handlers.py      (← 전체)               │
                                              │
Layer 4 (통합)                                │
  main.py ←── handlers + config ──────────────┘
```

---

## 개발 순서

### Step 0: 프로젝트 초기화

| 항목 | 내용 |
|------|------|
| 작업 | pyproject.toml, uv 환경, 디렉토리 구조 생성 |
| agent 전략 | 단일 작업, sub-agent 불필요 |
| 완료 조건 | `uv sync` 성공, 디렉토리 구조 일치 |

### Step 1: Layer 0 — 설정 + 저장소

| 항목 | 내용 |
|------|------|
| 작업 | config.py, models.py (DDL), repository.py (CRUD) |
| agent 전략 | 순차 개발 (models → repository 의존) |
| 테스트 | DB 초기화, CRUD 동작, 암호화/복호화 |
| 리뷰 | 스키마가 설계 문서 데이터 모델과 일치하는지 검증 |

### Step 2: Layer 1 — 도구 + 필터

| 항목 | 내용 |
|------|------|
| 작업 | search.py, scraper.py, publisher.py |
| agent 전략 | **병렬 sub-agent 3개** (모듈 간 의존 없음) |
| 테스트 | Naver API 실 호출, HTML 파싱, 필터 정확도 |
| 리뷰 | 각 모듈이 독립적으로 동작하는지 검증 |

병렬 분배:
```
sub-agent A: tools/search.py    — Naver API 호출 + 시간 윈도우 필터
sub-agent B: tools/scraper.py   — httpx + BeautifulSoup 본문 추출
sub-agent C: filters/publisher.py — publishers.json 로드 + 도메인 매칭
```

### Step 3: Layer 2+3 — 분석 + 봇

| 항목 | 내용 |
|------|------|
| 작업 | check_agent.py, formatters.py, conversation.py, handlers.py |
| agent 전략 | 순차 개발 (의존 체인이 강함) |
| 순서 | formatters → check_agent → conversation → handlers |
| 테스트 | Claude API mock 분석, 메시지 포맷, 대화 상태 전이 |
| 리뷰 | /check 8단계 흐름이 설계 문서와 일치하는지 검증 |

### Step 4: Layer 4 — 통합 + 수동 검증

| 항목 | 내용 |
|------|------|
| 작업 | main.py 작성, 전체 연결 |
| agent 전략 | 단일 작업 |
| 테스트 | 통합 테스트 + 실제 Telegram 수동 검증 |
| 리뷰 | 최종 코드 리뷰 (전체) |

---

## Agent 활용 전략

| 단계 | 방식 | 근거 |
|------|------|------|
| Step 0 | 직접 실행 | 단순 초기화 |
| Step 1 | 순차 개발 | models → repository 의존 |
| Step 2 | **병렬 sub-agent (3개)** | search, scraper, publisher가 완전 독립 |
| Step 3 | 순차 개발 | 의존 체인 강함, 일관성 중요 |
| Step 4 | 직접 실행 | 통합 연결 |
| 리뷰 | **code-review agent** | 각 Step 완료 후 설계 문서 대비 검증 |

---

## 평가 체계

### 1. 자동 테스트 (pytest)

| 모듈 | 테스트 항목 |
|------|------------|
| config | 환경변수 로드, 기본값 처리 |
| models | 테이블 생성, 스키마 정합성 |
| repository | journalist CRUD, reported_articles 저장/조회, 시간 윈도우 |
| search | Naver API 응답 파싱, 시간 윈도우 필터링 (mock) |
| scraper | HTML → 본문 추출 (고정 HTML 샘플) |
| publisher | 화이트리스트 필터 정확도 (23개 전수) |
| check_agent | Claude 응답 파싱, 카테고리 분류 (mock) |
| formatters | 메시지 포맷 생성, 4096자 제한 |
| conversation | 상태 전이 (NAME→DEPARTMENT→KEYWORDS→API_KEY→DONE) |

### 2. 수동 검증 체크리스트

실제 Telegram에서 본인이 기자 역할로 테스트한다.

```
/start 검증:
  [ ] /start 입력 → 이름 질문
  [ ] 이름 입력 → 부서 Inline Keyboard 표시
  [ ] 부서 선택 → 키워드 질문
  [ ] 키워드 입력 → API 키 질문
  [ ] API 키 입력 → 설정 완료 메시지 + DB 저장 확인
  [ ] /start 재실행 → 기존 프로필 덮어쓰기

/check 검증:
  [ ] /check 실행 → 헤더 메시지 수신 ("주요 N건 (전체 M건 중)")
  [ ] 기사별 개별 메시지 수신 ([단독]/[주요] 태그 포함)
  [ ] [스킵] 기사는 메시지로 오지 않음
  [ ] 각 기사에 요약 + 판단 근거 + URL 포함
  [ ] 즉시 재실행 → 시간 윈도우 축소되어 결과 적거나 없음
  [ ] 30분 후 재실행 → 30분 윈도우 내 신규 기사만 수집
  [ ] 동일 사안 재보고 안 됨 (중복 제거 동작)

/setkey 검증:
  [ ] /setkey 입력 → 새 API 키 입력 → 변경 완료
  [ ] 변경 후 /check → 새 키로 Claude 호출 성공
```

### 3. 코드 리뷰 체크포인트

| 시점 | 검증 항목 |
|------|----------|
| Step 1 완료 | DB 스키마 = 설계 문서 데이터 모델, 암호화 동작 |
| Step 2 완료 | 각 도구 독립 동작, 에러 처리 |
| Step 3 완료 | /check 8단계 흐름 = 설계 문서, Claude 프롬프트 = 설계 문서 |
| Step 4 완료 | 전체 통합, 수동 체크리스트 통과 |

---

## Phase 1 완료 기준

아래 조건을 **모두** 충족해야 Phase 1 완료:

1. `pytest` 전체 통과
2. 수동 검증 체크리스트 전항 통과
3. /start → /check → /setkey 전체 플로우 정상 동작
4. 코드 리뷰 4회 완료 (Step별 1회)
5. 설계 문서와 구현 간 차이점 문서화 (있을 경우)

---

## 개발 환경

| 항목 | 설정 |
|------|------|
| 실행 환경 | 로컬 맥북 (python main.py) |
| Python | 3.12+ (uv 관리) |
| Telegram | @tasa_check_bot (polling 모드) |
| Naver API | .env의 Client ID/Secret |
| Anthropic API | 본인 키로 테스트 |
| DB | 로컬 SQLite (data/tasa-check.db) |
