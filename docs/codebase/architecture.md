# tasa-check 시스템 아키텍처

## 1. 시스템 목적과 설계 철학

tasa-check는 기자를 위한 타사 뉴스 모니터링 텔레그램 봇이다. 네이버 뉴스 검색 API로 기사를 수집하고, Claude LLM으로 분석하여 중요 기사를 식별해 텔레그램으로 전송한다.

### 1.1 BYOK (Bring Your Own Key) 정책

기자가 자신의 Anthropic API 키를 `/start` 등록 과정에서 직접 입력한다. 키는 `src/storage/repository.py`의 `encrypt_api_key()` 함수로 Fernet 대칭 암호화하여 DB에 저장되며, LLM 호출 시 `decrypt_api_key()`로 복호화하여 사용한다. 이 구조로 개발자/운영자의 LLM 비용이 $0이 된다.

- 암호화 키: 환경변수 `FERNET_KEY`에서 로드 (`src/config.py`)
- 키 입력 메시지 보안: 입력 직후 `update.message.delete()` 호출로 텔레그램 채팅방에서 삭제 시도 (`src/bot/conversation.py:receive_api_key`, `src/bot/settings.py:receive_apikey`)

### 1.2 적응형 시간 윈도우

`/check` 명령은 마지막 체크 시각(`last_check_at`) 기반으로 검색 범위를 동적 결정한다.

```
now = datetime.now(UTC)
last_check → last_dt
window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
since = now - timedelta(seconds=window_seconds)
```

- 최대값: `CHECK_MAX_WINDOW_SECONDS = 10800` (3시간, `src/config.py`)
- 첫 실행(`last_check_at`이 NULL)일 때도 최대값(3시간) 적용
- 파이프라인 완료 후 `repo.update_last_check_at()`으로 현재 시각 갱신

`/report`도 적응형 시간 윈도우를 사용한다. `last_report_at` 기반으로 마지막 리포트 시각부터 현재까지의 범위를 동적으로 결정하며, 최대값은 `REPORT_MAX_WINDOW_SECONDS = 10800` (3시간)이다. 첫 실행(`last_report_at`이 NULL)일 때 최대값 적용.

### 1.3 비용 최적화

`/report` 파이프라인은 에이전트 루프 없이 3단계 1-shot 호출로 설계되었다.

1. 네이버 API로 기사 수집 (LLM 비용 $0)
2. Haiku(`claude-haiku-4-5-20251001`)로 LLM 필터 1회 --- 제목+description만으로 부서 무관 기사 제거
3. Haiku(`claude-haiku-4-5-20251001`)로 분석 1회 --- 필터 통과 기사에 대해 본문 포함 분석 (5회 재시도, temperature 점진적 증가)

두 LLM 호출 모두 `tool_choice={"type": "tool", "name": "..."}` 지정으로 강제 tool_use하여, 단일 턴에서 구조화된 응답을 받는다. 에이전트 루프(multi-turn)가 없으므로 토큰 누적이 발생하지 않는다.

### 1.4 리소스 제약 대응

Oracle Cloud Free Tier VM (1GB RAM)에서 운영된다. OOM 방지를 위한 리소스 관리 장치:

- 전역 파이프라인 세마포어: `_pipeline_semaphore = asyncio.Semaphore(5)` (`src/bot/handlers.py`) --- 동시 5개 파이프라인 제한
- 사용자별 락: `_user_locks: dict[str, asyncio.Lock]` --- 동일 유저의 동시 요청 방지
- 스크래핑 세마포어: `_scrape_semaphore = asyncio.Semaphore(50)` (`src/tools/scraper.py`) --- 동시 HTTP 요청 50개 제한
- 캐시 자동 정리: `CACHE_RETENTION_DAYS = 5` --- 5일 초과 데이터 삭제

---

## 2. 모듈 의존 관계

```
src/
  config.py                 # 환경변수, 부서 프로필
  storage/
    models.py               # DDL, DB 초기화
    repository.py           # CRUD 함수 전체
  tools/
    search.py               # 네이버 뉴스 검색
    scraper.py              # 기사 본문 스크래핑
  filters/
    publisher.py            # 언론사 화이트리스트 필터
  agents/
    check_agent.py          # /check LLM 분석
    report_agent.py         # /report LLM 필터 + 분석
  bot/
    conversation.py         # /start 프로필 등록 ConversationHandler
    handlers.py             # /check, /report, /set_division, /status, /stats 핸들러
    settings.py             # /set_keyword, /set_apikey, /set_schedule 설정 변경 ConversationHandler
    formatters.py           # 텔레그램 메시지 포맷팅
    scheduler.py            # JobQueue 관리 + 자동 실행 콜백
main.py                     # 진입점
data/
  publishers.json           # 언론사 화이트리스트 (27개)
  tasa-check.db             # SQLite 데이터베이스 (기본 경로)
```

### 2.1 레이어 구조

#### Layer 0 --- 기반

외부 의존이 없는 설정과 데이터 계층.

| 모듈 | 역할 | 주요 요소 |
|------|------|-----------|
| `src/config.py` | 환경변수 로드, 상수 정의 | `TELEGRAM_BOT_TOKEN`, `NAVER_CLIENT_ID/SECRET`, `FERNET_KEY`, `DB_PATH`, `CHECK_MAX_WINDOW_SECONDS`, `REPORT_MAX_WINDOW_SECONDS`, `CACHE_RETENTION_DAYS`, `ADMIN_TELEGRAM_ID`, `DEPARTMENT_PROFILES`, `DEPARTMENTS` |
| `src/storage/models.py` | DDL 정의, DB 연결 초기화 | `init_db()` --- 테이블 5개 생성 + 마이그레이션 |
| `src/storage/repository.py` | 전체 CRUD 함수 | Fernet 암복호화, journalists/reported_articles/report_cache/report_items/schedules 조회/저장/삭제 |

[상세: storage.md]

#### Layer 1 --- 도구

외부 서비스와 통신하는 데이터 수집/필터 계층. Layer 0의 `config.py`만 참조한다.

| 모듈 | 역할 | 의존 |
|------|------|------|
| `src/tools/search.py` | 네이버 뉴스 검색 API 호출 | `config.NAVER_CLIENT_ID/SECRET` |
| `src/tools/scraper.py` | 네이버 뉴스 본문 스크래핑 | (독립) |
| `src/filters/publisher.py` | 언론사 화이트리스트 필터링 | `config.BASE_DIR` (publishers.json 경로) |

#### Layer 2 --- 에이전트

LLM 호출을 담당하는 분석 계층. Layer 0의 `config.py`와 Layer 1의 `publisher.py`를 참조한다.

| 모듈 | 역할 | 의존 |
|------|------|------|
| `src/agents/check_agent.py` | `/check` Haiku 사전 필터 + 분석 (Haiku 2회) | `config.DEPARTMENT_PROFILES`, Langfuse |
| `src/agents/report_agent.py` | `/report` LLM 필터 (Haiku 1회) + 분석 (Haiku 1회, 5회 재시도) | `config.DEPARTMENT_PROFILES`, `publisher.get_publisher_name`, Langfuse |

#### Layer 3 --- 봇

텔레그램 인터페이스 계층. Layer 0~2 전체를 오케스트레이션한다.

| 모듈 | 역할 | 의존 |
|------|------|------|
| `src/bot/formatters.py` | 텔레그램 HTML 메시지 생성 | (독립, 순수 포맷팅) |
| `src/bot/conversation.py` | `/start` 프로필 등록 상태 머신 | `config.DEPARTMENTS`, `repository` |
| `src/bot/handlers.py` | `/check`, `/report`, `/set_division`, `/status`, `/stats` 핸들러 + 파이프라인 | `search`, `scraper`, `publisher`, `check_agent`, `report_agent`, `repository`, `formatters` |
| `src/bot/settings.py` | `/set_keyword`, `/set_apikey`, `/set_schedule` 설정 변경 ConversationHandler | `repository`, `scheduler` |
| `src/bot/scheduler.py` | JobQueue 자동 실행 콜백 + 서버 재시작 복원 | `handlers._run_check_pipeline`, `handlers._run_report_pipeline`, `handlers._handle_report_scenario_*`, `repository`, `formatters` |

#### Layer 4 --- 진입점

| 모듈 | 역할 |
|------|------|
| `main.py` | `Application` 구성, 핸들러 등록, `post_init`(DB 초기화 + 스케줄 복원 + 일일 정리 등록), `post_shutdown`(DB 닫기), `run_polling()` 시작 |

### 2.2 의존 흐름 요약

```
Layer 4  main.py
           |
Layer 3  handlers.py ──── scheduler.py
           |     |            |
           |     +--- conversation.py
           |     +--- settings.py
           |     +--- formatters.py
           |
Layer 2  check_agent.py    report_agent.py
           |                   |
Layer 1  search.py  scraper.py  publisher.py
           |                       |
Layer 0  config.py  models.py  repository.py
```

---

## 3. 외부 API 의존성

### 3.1 Naver News Search API

- 엔드포인트: `https://openapi.naver.com/v1/search/news.json`
- 인증: `X-Naver-Client-Id` / `X-Naver-Client-Secret` 헤더 (환경변수 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`)
- 호출 위치: `src/tools/search.py`
- 페이징: `_DISPLAY = 100` (1회 100건), `_MAX_PAGES = 2` (최대 2페이지 = 200건/키워드)
- 총 상한: `/check`는 `max_results=300`, `/report`도 `max_results=300` 전달
- Rate limit 대응: `_BATCH_SIZE = 3` (동시 3개 키워드), `_BATCH_DELAY = 0.5`초 배치 간 대기, 429 발생 시 `_RETRY_MAX = 2`회 재시도 (지수 백오프 `_RETRY_DELAY * (attempt + 1)`)

### 3.2 Anthropic Claude API

- 모델: Haiku 4.5 (`claude-haiku-4-5-20251001`) 단일 모델 사용
- 인증: 기자 개인의 API 키 (BYOK)
- 호출 방식: `tool_use` 강제 --- `tool_choice={"type": "tool", "name": "submit_analysis|submit_report|filter_news"}`
- 호출 위치 및 모델:
  - `src/agents/check_agent.py:filter_check_articles` --- Haiku 4.5, `max_tokens=2048`, `temperature=0.0`
  - `src/agents/check_agent.py:analyze_articles` --- Haiku 4.5, `max_tokens=16384`, `temperature=0.0~0.4` (5회 재시도)
  - `src/agents/report_agent.py:filter_articles` --- Haiku 4.5, `max_tokens=2048`, `temperature=0.0`
  - `src/agents/report_agent.py:analyze_report_articles` --- Haiku 4.5, `max_tokens=16384`, `temperature=0.0~0.4` (5회 재시도)

### 3.3 Telegram Bot API

- 라이브러리: `python-telegram-bot[job-queue]>=21.0`
- 인증: 환경변수 `TELEGRAM_BOT_TOKEN`
- 동시성: `Application.builder().concurrent_updates(True)` --- 핸들러 병렬 실행 허용
- 메시지 포맷: Telegram HTML (`parse_mode="HTML"`)
- 메시지 길이 제한: `_MAX_MSG_LEN = 4096` (`src/bot/formatters.py`)

### 3.4 Langfuse (선택적)

- LLM 호출 모니터링 및 트레이싱
- 환경변수 `LANGFUSE_PUBLIC_KEY`가 설정된 경우에만 활성화 (`main.py`에서 조건부 임포트)
- `AnthropicInstrumentor().instrument()` --- Anthropic API 호출 자동 계측
- 에이전트 내부에서 `langfuse.start_as_current_observation()` 스팬 생성 (이름: `check_filter`, `check_agent`, `report_filter`, `report_agent`)

---

## 4. /check와 /report 파이프라인

### 4.1 /check 파이프라인

사용자의 개인 키워드 기반 타사 체크. 핵심 함수: `src/bot/handlers.py:_run_check_pipeline`.

```
[1] 프로필 로드
    repo.get_journalist(db, telegram_id) → journalist dict
    ↓
[2] 시간 윈도우 계산
    last_check_at 기반 적응형 since 결정 (최대 3시간)
    ↓
[3] 네이버 뉴스 검색
    search_news(journalist["keywords"], since, max_results=300)
    ↓
[4] 언론사 필터
    filter_by_publisher(raw_articles) → publishers.json 화이트리스트 매칭
    ↓
[5] 제목 기반 필터
    _SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}
    위 태그가 제목에 포함된 기사 제거
    ↓
[6] Haiku 사전 필터
    filter_check_articles(api_key, articles, keywords, department)
    키워드 관련성 기반 사전 필터링 (제목+description만 사용, 본문 스크래핑 전)
    ↓
[7] 본문 스크래핑
    fetch_articles_batch(urls) → 네이버 뉴스 URL에서 본문 추출 (최대 800자)
    Haiku 통과 기사만 스크래핑
    ↓
[8] Claude 분석
    analyze_articles(api_key, articles, history, department, keywords) → Haiku 4.5 호출 (5회 재시도)
    - history: repo.get_recent_reported_articles(hours=72) --- 최근 72시간 보고 이력
    - tool_use로 submit_analysis 도구 강제 호출
    - 응답: results(exclusive/important) + skipped(skip) 병합 반환
    ↓
[9] 결과 역매핑 + 저장 + 전송
    source_indices → url, publisher, pub_time, source_count 주입
    repo.save_reported_articles() → DB 저장
    최신 기사 우선(pub_time desc) 정렬 후 텔레그램 전송
    last_check_at 갱신
```

Claude 시스템 프롬프트 구조 (`check_agent.py:_SYSTEM_PROMPT_TEMPLATE`):
- 부서 취재 영역, 기자 키워드, 키워드 관련성 필터, 부서별 판단 기준
- 단독 기사 식별 규칙 (제목 태그 + 본문 어미 패턴)
- 중복 제거 기준 (배치 내 + 이전 보고 대비)
- skip 승격 제한 규칙

출력 분류:
- `exclusive`: 단독 기사 (제목 [단독] 태그 또는 사실상 단독)
- `important`: 주요 기사
- `skip`: 스킵 (키워드 무관, 중복, 가치 부족)

### 4.2 /report 파이프라인

부서 단위 뉴스 브리핑. 핵심 함수: `src/bot/handlers.py:_run_report_pipeline`.

```
[1] 프로필 로드 + 부서 키워드 결정
    DEPARTMENT_PROFILES[dept_label]["report_keywords"] 사용 (사용자 개인 키워드가 아님)
    예: 사회부 → ["경찰 수사", "검찰 기소", "법원 판결", ...] (13개)
    ↓
[2] 네이버 뉴스 검색
    search_news(report_keywords, since, max_results=300)
    since = last_report_at 기반 적응형 (최대 3시간)
    ↓
[3] 언론사 필터
    filter_by_publisher() → publishers.json 화이트리스트 매칭
    ↓
[4] Haiku LLM 필터 (1회 호출)
    filter_articles(api_key, filtered, department) → Haiku 4.5
    - 제목 + description만으로 판단 (본문 스크래핑 전)
    - 부서 무관 기사, 사진 캡션, 중복 사안 제거
    - tool_use로 filter_news 도구 강제 호출 → selected_indices 반환
    ↓
[5] 본문 스크래핑
    fetch_articles_batch(urls) → 필터 통과 기사만 스크래핑 (비용 절감)
    ↓
[6] Claude 분석 (1회 호출)
    analyze_report_articles(api_key, articles, report_history, existing_items, department)
    → Haiku 4.5 (5회 재시도, temperature 점진적 증가)
    - report_history: repo.get_recent_report_items(days=2) --- 최근 2일 보고 이력
    - 시나리오 분기: existing_items 유무에 따라 A/B 시나리오
    - tool_use로 submit_report 도구 강제 호출
    ↓
[7] 결과 역매핑 + 저장 + 전송
    source_indices → url, publisher, pub_time, source_count 주입
    URL은 a["link"] (네이버 뉴스 링크) 사용
```

#### 시나리오 A/B 분기

`/report`는 당일 캐시 존재 여부에 따라 시나리오가 나뉜다.

**시나리오 A (당일 첫 요청)**:
- `repo.get_or_create_report_cache()` → `is_new=True`
- LLM에 기존 캐시 없이 분석 요청
- 결과를 `repo.save_report_items()`로 저장
- 전체 브리핑 출력

**시나리오 B (당일 재요청)**:
- `repo.get_or_create_report_cache()` → `is_new=False`
- `repo.get_report_items_by_cache()` → 기존 캐시 항목 로드
- LLM에 기존 캐시를 함께 전달하여 변경분만 분석 요청
- LLM 응답의 `action` 필드: `"modified"` (기존 항목 갱신) / `"added"` (신규 추가)
- 기존 항목 + 변경분을 병합하여 전체 브리핑 출력
- 출력 시 `[수정]`/`[신규]` 태그 표시, 변경 항목 우선 정렬

report_items 출력 분류:
- `category`: `"follow_up"` (이전 보도 후속) / `"new"` (신규)
- `exclusive`: 단독 기사 여부 (boolean)

---

## 5. 데이터베이스 스키마

SQLite (`aiosqlite`), 테이블 5개. DDL은 `src/storage/models.py`에 정의.

| 테이블 | 용도 |
|--------|------|
| `journalists` | 기자 프로필 (telegram_id, department, keywords, api_key, last_check_at, last_report_at) |
| `reported_articles` | `/check` 분석 결과 (topic_cluster, key_facts, summary, article_urls, category, reason) |
| `report_cache` | `/report` 당일 캐시 헤더 (journalist_id, date) --- 시나리오 A/B 판단용 |
| `report_items` | `/report` 브리핑 항목 (title, url, summary, tags, category, reason, exclusive, publisher, pub_time, key_facts, source_count) |
| `schedules` | 자동 실행 예약 (journalist_id, command, time_kst) |

[상세: storage.md]

---

## 6. 프로덕션 운영 요소

### 6.1 동시성 제어

```python
# src/bot/handlers.py
_user_locks: dict[str, asyncio.Lock] = {}        # 사용자별 락
_pipeline_semaphore = asyncio.Semaphore(5)        # 전역 파이프라인 5개 제한

# src/tools/scraper.py
_scrape_semaphore = asyncio.Semaphore(50)         # 전역 동시 스크래핑 50개 제한
```

- 사용자별 락: `_user_locks.setdefault(telegram_id, asyncio.Lock())`. `lock.locked()` 체크 후 이미 실행 중이면 즉시 반환.
- 파이프라인 세마포어: `/check`와 `/report` 모두 `async with _pipeline_semaphore:` 내에서 실행. 동시 5개 초과 시 대기.
- 스크래퍼 세마포어: `fetch_articles_batch()` 내부의 `_fetch_one()`이 개별 HTTP 요청마다 `async with _scrape_semaphore:` 사용.
- `scheduler.py`의 `scheduled_check`/`scheduled_report`도 동일한 `_user_locks`와 `_pipeline_semaphore`를 공유하여 수동/자동 실행 간 충돌을 방지한다.

### 6.2 Langfuse 트레이싱

`main.py`에서 `LANGFUSE_PUBLIC_KEY` 환경변수 존재 시 `AnthropicInstrumentor().instrument()`로 Anthropic 호출을 자동 계측한다. 에이전트 내부에서 추가 스팬을 생성한다:

- `check_filter`: `metadata={"department": department, "input_count": len(articles)}`
- `check_agent`: `metadata={"department": department, "attempt": attempt + 1}`
- `report_filter`: `metadata={"department": department, "input_count": len(articles)}`
- `report_agent`: `metadata={"department": department, "scenario": "A"|"B", "attempt": attempt + 1}`

### 6.3 일일 캐시 정리

```python
# main.py:post_init
application.job_queue.run_daily(
    _daily_cleanup, time=time(hour=4, minute=0, tzinfo=_KST), name="daily_cleanup",
)
```

매일 04:00 KST에 `repo.cleanup_old_data(db)` 실행:
- `CACHE_RETENTION_DAYS = 5`일 초과 데이터 삭제
- 대상: `report_items` (report_cache 기준), `report_cache`, `reported_articles`
- 서버 시작 시에도 즉시 1회 실행 (`post_init`에서 `await cleanup_old_data(db)`)

### 6.4 서버 재시작 시 스케줄 복원

```python
# main.py:post_init
await restore_schedules(application, db)
```

`scheduler.py:restore_schedules()`가 `repo.get_all_schedules(db)`로 DB의 전체 스케줄을 조회한 뒤, 각각 `register_job()`으로 `app.job_queue.run_daily()`에 재등록한다. 서버 재시작 시 기존 사용자의 스케줄이 유실되지 않는다.

### 6.5 스케줄 제한

```python
# src/bot/scheduler.py
_MAX_TIMES = {"check": 30, "report": 30}
```

- `/set_schedule check`: 최대 30개 시각 등록 가능
- `/set_schedule report`: 최대 30개 시각 등록 가능
- 스케줄 관리는 `src/bot/settings.py`의 `build_settings_handler()`에서 `/set_schedule` ConversationHandler로 처리

### 6.6 관리자 기능

`ADMIN_TELEGRAM_ID = "8571411084"` (`src/config.py`)로 지정된 텔레그램 ID만 `/stats` 명령 사용 가능. `repo.get_admin_stats(db)`로 전체 사용자 수, 부서별 분포, 스케줄 현황, 사용자별 상세를 조회한다.

---

## 7. 부서 프로필 시스템

`src/config.py`의 `DEPARTMENT_PROFILES` dict에 8개 부서가 정의되어 있다: 사회부, 정치부, 경제부, 산업부, 테크부, 문화부, 스포츠부, 국제부.

각 부서 프로필 구조:

| 키 | 용도 | 사용 위치 |
|----|------|-----------|
| `coverage` | 취재 영역 텍스트 | `check_agent`, `report_agent` 시스템 프롬프트 |
| `criteria` | 중요 기사 판단 기준 리스트 | `check_agent`, `report_agent` 시스템 프롬프트 |
| `report_keywords` | `/report`용 검색 키워드 리스트 | `handlers._run_report_pipeline`에서 네이버 API 검색어로 사용 |

`/check`는 `journalist["keywords"]` (사용자 개인 키워드)로 검색하고, `/report`는 `DEPARTMENT_PROFILES[dept_label]["report_keywords"]` (부서 공통 키워드)로 검색한다. 이 차이가 두 명령의 핵심 설계 분기점이다.

---

## 8. 본문 스크래핑 구조

`src/tools/scraper.py`는 네이버 뉴스(`n.news.naver.com`) 기사 본문에서 최대 800자를 추출한다.

- 본문 컨테이너: `article#dic_area` 또는 `div#newsct_article`
- 문단 추출: `<p>` 태그에서 사진/이미지 래퍼 안의 캡션 제외
- 소제목 필터링 (`_is_subheading`):
  - `_SUBHEADING_MARKERS`: 특수 마커 문자 집합 (▶, ■, ◆, ● 등)
  - 전체 볼드 + 50자 미만 텍스트 = 소제목
- 상한: `_MAX_CHARS = 800` (글자 수 기반, 문단 수 기반이 아님)

배치 스크래핑 (`fetch_articles_batch`):
- `httpx.AsyncClient` 공유로 연결 재사용
- `_scrape_semaphore` (50개)로 동시 요청 제한
- 타임아웃: `_TIMEOUT = 10.0`초

---

## 9. 텔레그램 메시지 포맷

`src/bot/formatters.py`에서 Telegram HTML 포맷의 메시지를 생성한다.

### /check 메시지

- `format_check_header`: 같은 날이면 시각만, 다른 날이면 날짜+시각 표시. `"타사 체크 (HH:MM ~ HH:MM)\n주요 N건 / 전체 M건"`
- `format_article_message`: 제목 전체가 하이퍼링크. `● [단독] [언론사] 제목 (HH:MM)` 형태. 요약과 판단 근거는 `<blockquote expandable>` 안에 표시. `source_count > 1`이면 `[언론사 등 다수]` 표시
- `format_skipped_articles(skipped, haiku_filtered)`: 스킵 기사 목록을 `<blockquote expandable>` 안에 표시. `topic_cluster` 기준 중복 제거. 반환형은 `list[str]` (4096자 초과 시 여러 메시지로 분할). `haiku_filtered`가 있으면 헤더에 표시

### /report 메시지

- `format_report_header_a` (시나리오 A): `"OO부 주요 뉴스 (날짜)\n주요 N건"`
- `format_report_header_b` (시나리오 B): `"OO부 주요 뉴스 (날짜)\n총 N건 (수정 X건, 추가 Y건)"`
- `format_report_item`: 태그 표시 규칙 --- `[단독]`, `[수정]`/`[신규]` (시나리오 B), `[후속]` (follow_up)
- `format_unchanged_report_items`: 시나리오 B에서 변경 없는 기존 항목을 `<blockquote expandable>` 안에 `기보고 N건` 형태로 표시

모든 메시지는 `_MAX_MSG_LEN = 4096`자 초과 시 분할 또는 잘린다.

---

## 10. 프로필 등록 흐름

`src/bot/conversation.py`의 `ConversationHandler` 상태 머신.

```
/start → DEPARTMENT 상태 (부서 선택 InlineKeyboard)
       → KEYWORDS 상태 (키워드 텍스트 입력, 쉼표 구분)
       → API_KEY 상태 (Anthropic API 키 입력)
       → 완료 (DB 저장, 기존 데이터 초기화)
```

- 상태 상수: `DEPARTMENT, KEYWORDS, API_KEY = range(3)`
- `upsert_journalist()`: `ON CONFLICT(telegram_id) DO UPDATE`로 재등록 시 기존 프로필 덮어쓰기
- 등록 완료 시 `clear_journalist_data()`로 기존 report/check 데이터 초기화 (스케줄은 유지)
- `/cancel`로 대화 중단 가능

---

## 11. 환경변수 목록

| 변수 | 필수 | 용도 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | O | 텔레그램 봇 토큰 |
| `NAVER_CLIENT_ID` | O | 네이버 API Client ID |
| `NAVER_CLIENT_SECRET` | O | 네이버 API Client Secret |
| `FERNET_KEY` | O | API 키 암호화용 Fernet 키 |
| `DB_PATH` | X | SQLite DB 경로 (기본값: `data/tasa-check.db`) |
| `LANGFUSE_PUBLIC_KEY` | X | Langfuse 트레이싱 활성화 |
| `LANGFUSE_SECRET_KEY` | X | Langfuse 인증 |
| `LANGFUSE_HOST` | X | Langfuse 서버 주소 |
