# Bot Layer

Telegram Bot의 사용자 인터페이스 계층. 커맨드 핸들러, 프로필 등록 대화, 메시지 포맷팅, 자동 실행 스케줄러, 애플리케이션 진입점으로 구성된다.

---

## 1. handlers.py 상세 해부

파일: `src/bot/handlers.py`

모든 주요 커맨드의 핸들러와 파이프라인 실행 로직을 담당한다.

### 1.1 동시성 제어

두 가지 메커니즘으로 서버 자원을 보호한다.

```python
_user_locks: dict[str, asyncio.Lock] = {}
_pipeline_semaphore = asyncio.Semaphore(5)
```

**`_user_locks`** -- 사용자별 `asyncio.Lock` 딕셔너리. telegram_id를 키로 사용하며, 한 사용자가 동시에 두 개의 파이프라인을 실행하는 것을 방지한다. `lock.locked()`로 이미 실행 중인지 확인한 뒤, 실행 중이면 `"이전 요청이 처리 중입니다. 완료 후 다시 시도해주세요."` 메시지를 반환하고 즉시 종료한다.

**`_pipeline_semaphore`** -- 전역 `asyncio.Semaphore(5)`. 서버 전체에서 동시에 실행되는 파이프라인 수를 최대 5개로 제한한다. Oracle Cloud 1GB RAM 인스턴스에서 Claude API 호출 + 네이버 뉴스 스크래핑이 동시에 다수 실행되면 OOM이 발생하므로, 세마포어로 병렬 실행 수를 통제한다.

모든 핸들러는 `async with lock:` 안에서 `async with _pipeline_semaphore:` 를 중첩하여 사용한다. 세마포어 해제 후에 결과 전송을 수행하므로, 무거운 작업(네이버 검색 + Claude 분석)만 세마포어 안에서 실행되고, 텔레그램 메시지 전송은 세마포어 밖에서 수행된다.

### 1.2 /check 커맨드 -- check_handler()

```python
async def check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

처리 흐름:

1. `_user_locks`에서 해당 사용자의 Lock 확인. 이미 잠겨 있으면 메시지 반환 후 종료
2. `repo.get_journalist(db, telegram_id)`로 프로필 로드. 미등록이면 `/start` 안내
3. Lock 획득 후 `"타사 체크 진행 중..."` 메시지 전송
4. 세마포어 획득 후 `_run_check_pipeline()` 실행
5. `repo.update_last_check_at()`로 마지막 체크 시각 갱신 (결과 유무와 무관하게 항상 갱신)
6. 결과가 없으면 `format_no_results()` 메시지 반환
7. 결과를 `reported` (category != "skip")와 `skipped` (category == "skip")로 분리
8. `repo.save_reported_articles()`로 전체 결과 DB 저장
9. `format_check_header()`로 헤더 메시지 전송
10. `sorted_reported`를 `pub_time` 역순(최신 먼저)으로 정렬하여 기사별 메시지 전송
11. 스킵 기사가 있으면 `format_skipped_articles()`로 접힌 목록 전송

### 1.3 /report 커맨드 -- report_handler()

```python
async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

시나리오 판별 로직:

1. Lock/프로필 검사 (check_handler와 동일)
2. `"브리핑 생성 중..."` 메시지 전송
3. KST 기준 오늘 날짜(`today`)를 `datetime.now(_KST).strftime("%Y-%m-%d")`로 계산
4. `repo.get_or_create_report_cache(db, journalist["id"], today)`로 당일 캐시 존재 여부 확인
   - 반환값: `(cache_id, is_new)` -- `is_new`가 True면 당일 첫 요청
5. 시나리오 A 조건: `is_new`이거나 기존 항목이 0건
6. 시나리오 B 조건: 기존 캐시에 항목이 존재하는 재요청
7. 세마포어 내에서 `_run_report_pipeline()` 실행
   - 시나리오 A면 `existing_items=None` 전달
   - 시나리오 B면 기존 항목 리스트 전달

**시나리오 A 처리 -- `_handle_report_scenario_a()`**

당일 첫 요청. `repo.save_report_items()`로 결과 저장 후, `pub_time` 역순으로 정렬하여 헤더 + 개별 아이템 메시지를 순차 전송한다.

**시나리오 B 처리 -- `_handle_report_scenario_b()`**

당일 재요청. delta_results에서 `action` 필드를 확인하여 기존 항목과 병합한다.

- `action == "modified"`: 기존 항목의 summary, reason, exclusive를 갱신
- `action == "added"`: 신규 항목으로 추가
- 변경 없는 기존 항목: `action = "unchanged"`로 표기

DB 반영:
- 추가 항목은 `repo.save_report_items()`로 저장
- 수정 항목은 `repo.update_report_item()`으로 개별 갱신

정렬 규칙 (안정 정렬 2단계):
1. `pub_time` 역순 정렬
2. `action_order` 기준 정렬 -- `modified`/`added`는 0, `unchanged`는 1
   - 변경/추가 항목이 상단, 기존 항목이 하단. 각 그룹 내에서는 시간 역순 유지

### 1.4 파이프라인 함수

#### _run_check_pipeline()

```python
async def _run_check_pipeline(db, journalist: dict) -> tuple[list[dict] | None, datetime, datetime, int]:
```

반환: `(분석 결과 리스트, since, now, haiku_filtered)`. 기사가 없으면 결과는 `None`. `haiku_filtered`는 Haiku 사전 필터에서 제거된 기사 수.

흐름:

1. **시간 윈도우 계산**: `last_check_at`이 있으면 마지막 체크 시점부터, 없으면 `CHECK_MAX_WINDOW_SECONDS` (3시간 = 10800초) 전부터. 최대 윈도우는 `CHECK_MAX_WINDOW_SECONDS`로 제한
2. **네이버 뉴스 수집**: `search_news(journalist["keywords"], since)`
3. **언론사 필터링**: `filter_by_publisher(raw_articles)` -- 자사 기사 제외
4. **제목 기반 필터링**: `_SKIP_TITLE_TAGS`에 해당하는 기사 제거
   ```python
   _SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}
   ```
5. **Haiku 사전 필터**: `filter_check_articles()` -- 부서 관련성 기반 사전 필터링 (제목+description만 사용)
6. **본문 수집**: `fetch_articles_batch(urls)` -- Haiku 통과 기사만 스크래핑 (최대 800자)
7. **분석용 데이터 조립**: title, publisher, body, url, pubDate 필드로 구성
8. **이전 체크 이력 로드**: `repo.get_recent_reported_articles(db, journalist["id"], hours=72)` -- 72시간 이내 이력
9. **Claude 분석**: `analyze_articles()` 호출 (Haiku, 5회 재시도). api_key, articles, history, department, keywords 전달
10. **인덱스 역매핑**: `_map_results_to_articles()` 헬퍼로 title 기반 매칭 우선, source_indices 폴백으로 URL, 언론사명, pub_time, source_count를 주입

#### _run_report_pipeline()

```python
async def _run_report_pipeline(
    db, journalist: dict, existing_items: list[dict] | None = None,
) -> list[dict] | None:
```

반환: 브리핑 항목 리스트. 기사가 없으면 `None`.

흐름:

1. **시간 윈도우**: `last_report_at` 기반 적응형 (최대 `REPORT_MAX_WINDOW_SECONDS` 3시간). 첫 실행 시 고정 3시간
2. **부서별 키워드 로드**: `DEPARTMENT_PROFILES`에서 `report_keywords` 추출. 부서명에 "부"가 없으면 자동 부착
3. **네이버 API 수집**: `search_news(report_keywords, since, max_results=300)` -- 최대 300건 상한
4. **언론사 필터**: `filter_by_publisher()`
5. **LLM 필터 (Haiku)**: `filter_articles()` -- 제목 + description 기반으로 Claude Haiku가 관련성 필터링
6. **본문 수집**: `fetch_articles_batch(urls)`
7. **분석용 데이터 조립**: check와 유사하나 `originallink`, `link` 필드를 추가로 포함
8. **이전 report 이력**: `repo.get_recent_report_items(db, journalist["id"])` -- 2일치
9. **Claude 분석**: `analyze_report_articles()` 호출. existing_items가 있으면(시나리오 B) 기존 항목 전달
10. **인덱스 역매핑**: `_map_results_to_articles()` 헬퍼로 title 기반 매칭 우선, source_indices 폴백으로 URL, 언론사명, pub_time, source_count를 주입. URL은 `src["link"]` (네이버 뉴스 URL) 사용. `item_id`는 순번→DB ID 변환

### 1.5 설정 변경 핸들러

`/set_apikey`, `/set_keyword`, `/set_schedule` 커맨드는 `src/bot/settings.py`의 `build_settings_handler()`가 생성하는 ConversationHandler에서 처리된다. 각 커맨드는 2단계(안내 -> 입력) 대화형으로 동작한다.

- `/set_apikey`: 현재 키 마스킹 표시 -> 새 키 입력 -> `sk-` 검증 -> 메시지 삭제 -> 저장
- `/set_keyword`: 현재 키워드 표시 -> 새 키워드 입력 -> 저장 + 체크 이력 초기화
- `/set_schedule`: 현재 스케줄 표시 -> 입력 (`check 09:00 12:00` 또는 `off`) -> 저장 + JobQueue 등록

#### set_division_handler() / set_division_callback()

```python
async def set_division_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
async def set_division_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

- `set_division_handler()`: InlineKeyboard로 부서 목록 표시. 콜백 데이터는 `setdiv:{부서명}` 형태
- `set_division_callback()`: 콜백 처리. `query.data.removeprefix("setdiv:")`로 부서명 추출
  - 동일 부서 선택 시 `"이미 {dept} 소속입니다."` 반환
  - `repo.update_department()`로 부서 변경
  - `repo.clear_journalist_data()`로 check/report 이력 전체 초기화 (스케줄은 유지)

### 1.6 stats_handler() -- 관리자 전용 통계

```python
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

- `ADMIN_TELEGRAM_ID` ("8571411084") 와 telegram_id 비교. 불일치하면 무응답(`return`)
- `repo.get_admin_stats(db)` 호출
- 출력 항목: 전체 사용자 수, 부서별 인원, 스케줄 등록 현황(check/report 건수), 사용자 목록(부서, 키워드, 스케줄 수, 최근 check 시각)
- last_check_at은 UTC를 KST로 변환하여 표시

### 1.6b status_handler() -- 현재 설정 조회

```python
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
```

- 프로필 미등록 검사
- 부서, 키워드, API Key(마스킹), check/report 스케줄을 표시

### 1.6c format_error_message() -- 에러 메시지 변환

Anthropic API 에러를 사용자 친화적 한국어 메시지로 변환하는 함수 (`src/bot/handlers.py`).
- 529: 서버 과부하 (5회 재시도 실패)
- 429: 요청 한도 초과
- 401: API 키 유효하지 않음
- 500+: 서버 오류
- APIConnectionError: 연결 실패
- APITimeoutError: 응답 시간 초과

### 1.7 에러 처리 방식

두 파이프라인 모두 세마포어 내부에서 `try/except Exception`으로 감싸며, 에러 발생 시:
- `logger.error()`로 exc_info 포함 로깅
- 사용자에게 에러 메시지 전송 (`f"타사 체크 중 오류가 발생했습니다: {e}"` 또는 `f"브리핑 생성 중 오류가 발생했습니다: {e}"`)
- `return`으로 핸들러 종료 (last_check_at 갱신 등 후속 작업 미실행)

---

## 2. conversation.py 상세 해부

파일: `src/bot/conversation.py`

프로필 등록을 위한 ConversationHandler. `/start` 커맨드로 진입한다.

### 2.1 상태 머신

```python
DEPARTMENT, KEYWORDS, API_KEY = range(3)
```

상태 흐름: `ENTRY` -> `DEPARTMENT` (0) -> `KEYWORDS` (1) -> `API_KEY` (2) -> `END`

### 2.2 build_conversation_handler()

```python
def build_conversation_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DEPARTMENT: [CallbackQueryHandler(receive_department)],
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keywords)],
            API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
```

- `entry_points`: `/start` 커맨드
- `DEPARTMENT` 상태: CallbackQueryHandler (InlineKeyboard 콜백)
- `KEYWORDS` 상태: TEXT 메시지 (커맨드 제외)
- `API_KEY` 상태: TEXT 메시지 (커맨드 제외)
- `fallbacks`: `/cancel` 커맨드로 등록 취소 가능

### 2.3 각 상태 핸들러

#### start()

```python
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
```

- `"타사 체크 봇입니다. 담당 부서를 선택해주세요."` 메시지와 함께 InlineKeyboard 표시
- `DEPARTMENTS` 리스트를 2열씩 묶어 버튼 배치
- 버튼의 `callback_data`는 부서명 문자열 그 자체 (예: `"사회부"`)
- `DEPARTMENT` 상태로 전이 반환

#### receive_department()

```python
async def receive_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
```

- `query.data`에서 선택된 부서명을 `context.user_data["department"]`에 저장
- 기존 메시지를 편집하여 부서 표시 + 키워드 입력 안내
  ```
  부서: {query.data}

  모니터링 키워드를 입력해주세요. (쉼표 구분)
  예: 서부지검, 서부지법, 영등포경찰서
  ```
- `KEYWORDS` 상태로 전이 반환

#### receive_keywords()

```python
async def receive_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
```

- 사용자 입력을 쉼표로 분리, 각 항목 strip 후 빈 문자열 필터링
- 키워드가 0개면 `"키워드를 1개 이상 입력해주세요. (쉼표 구분)"` 메시지 후 `KEYWORDS` 상태 유지
- `context.user_data["keywords"]`에 키워드 리스트 저장
- API 키 입력 안내: `"Anthropic API 키를 입력해주세요.\n(1:1 DM이므로 타인에게 노출되지 않습니다)"`
- `API_KEY` 상태로 전이 반환

#### receive_api_key()

```python
async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
```

- `sk-` 접두사 검증. 미충족시 재입력 요청 후 `API_KEY` 상태 유지
- 보안: `update.message.delete()`로 API 키가 포함된 메시지 삭제 시도 (실패해도 무시)
- `repo.upsert_journalist()`로 프로필 DB 저장 (신규 등록 또는 기존 프로필 갱신)
- `repo.clear_journalist_data()`로 기존 check/report 데이터 초기화 (스케줄은 유지)
- 완료 메시지에 사용 가능한 커맨드 목록 안내:

```
설정 완료!

부서: {department}
키워드: {keywords}

[실행]
/check - 키워드 기반 타사 체크
/report - 부서 주요 뉴스 브리핑

[자동 실행]
/set_schedule - 예약 설정

[설정 변경]
/set_apikey - Claude API 키 변경
/set_keyword - 모니터링 키워드 변경
/set_division - 부서 변경
```

- `context.user_data.clear()`로 대화 임시 데이터 정리
- `ConversationHandler.END` 반환으로 대화 종료

#### cancel()

`/cancel` 입력 시 `"등록이 취소되었습니다."` 메시지 전송, `context.user_data.clear()` 후 `ConversationHandler.END` 반환.

---

## 3. formatters.py 상세 해부

파일: `src/bot/formatters.py`

Telegram HTML 포맷 기반 메시지 생성 모듈. 공통 상수:

```python
_KST = timezone(timedelta(hours=9))
_MAX_MSG_LEN = 4096
```

모든 사용자 입력/Claude 응답 텍스트는 `html_module.escape()`로 HTML 이스케이프 처리한다.

**공통 유틸리티 함수:**

- `_publisher_label(publisher, source_count)` -- 복수 출처면 `[언론사 등 다수]`, 단일이면 `[언론사]` 반환
- `_split_blockquote_messages(header, item_lines)` -- header + blockquote expandable 메시지를 4096자 이내로 분할. `list[str]` 반환
- `_truncate(msg)` -- 4096자 초과 시 `msg[:4093] + "..."` 로 잘라냄

### 3.1 /check 포맷

#### format_check_header()

```python
def format_check_header(total: int, important: int, since: datetime, now: datetime) -> str:
```

- since, now를 KST로 변환. 같은 날이면 시각만(`HH:MM ~ HH:MM`), 다른 날이면 날짜+시각 표시
- 출력 형태:
  ```html
  🔍 <b>타사 체크</b> (09:00 ~ 12:00)
  주요 <b>3</b>건 / 전체 8건
  ```

#### format_article_message()

```python
def format_article_message(article: dict) -> str:
```

기사 1건을 HTML 메시지로 변환한다.

태그 매핑:
```python
tag_map = {"exclusive": "[단독]", "breaking": "[속보]"}
```

제목에 `[단독]` 태그가 이미 포함된 경우 중복 제거 처리. `_publisher_label()`로 복수 출처 시 `[언론사 등 다수]` 표시.

제목 라인 구성: `● {tag} {pub_label} {title} ({pub_time})`

메시지 구조:
```html
<b><a href="https://...">● [단독] [한국일보] 검찰, 대규모 비리 수사 착수 (14:30)</a></b>
<blockquote expandable>검찰이 대규모 비리 사건에 대한 본격 수사에 착수했다.
-> <i>자사 미보도 단독 기사로, 검찰 출입 기자 확인 필요</i></blockquote>
```

- `url` 필드를 링크로 사용. 제목 전체가 하이퍼링크
- 메시지 길이가 `_MAX_MSG_LEN` (4096자) 초과 시 `msg[:4093] + "..."` 로 잘라냄

#### format_no_results() / format_no_important()

- `format_no_results()`: `"시간 윈도우 내 신규 기사가 없습니다."`
- `format_no_important()`: `"키워드 관련 주요 기사가 없습니다."`

#### format_skipped_articles()

```python
def format_skipped_articles(skipped: list[dict], haiku_filtered: int = 0) -> list[str]:
```

- `topic_cluster` 기준 중복 제거: 동일 `topic_cluster` 값을 가진 기사는 1건만 표시
- `_publisher_label()`로 언론사 + 다수 표시 처리
- 각 항목을 `- {[언론사] 제목 (시각) 링크} → {스킵 사유}` 형태로 나열
- `_split_blockquote_messages()`로 4096자 이내 메시지 분할 (`list[str]` 반환)
- 출력 형태:
  ```html
  <b>스킵 5건</b> (사진/광고 기사 필터링 3건 외)
  <blockquote expandable>- <a href="...">[한국일보] 기사 제목 1 (14:30)</a> → 기보도 내용
  - <a href="...">[조선일보] 기사 제목 2 (15:00)</a> → 관련성 낮음</blockquote>
  ```

### 3.2 /report 포맷

#### _dept_label()

부서명에 "부"가 없으면 자동 부착하는 유틸리티 함수.

```python
def _dept_label(department: str) -> str:
    return department if department.endswith("부") else f"{department}부"
```

#### format_report_header_a()

```python
def format_report_header_a(department: str, date: str, count: int) -> str:
```

시나리오 A (당일 첫 요청) 헤더:
```html
📋 <b>사회부 주요 뉴스</b> (2025-01-15)
주요 <b>12</b>건
```

#### format_report_header_b()

```python
def format_report_header_b(department: str, date: str, total: int, modified: int, added: int) -> str:
```

시나리오 B (당일 재요청) 헤더. 변경 내역을 함께 표시:
```html
📋 <b>사회부 주요 뉴스</b> (2025-01-15)
총 <b>15</b>건 (수정 2건, 추가 3건)
```
변경이 없으면:
```html
📋 <b>사회부 주요 뉴스</b> (2025-01-15)
총 <b>12</b>건 (변경 없음)
```

#### format_report_item()

```python
def format_report_item(item: dict, scenario_b: bool = False) -> str:
```

브리핑 항목 1건을 HTML 메시지로 변환한다.

태그 결정 로직:
- `exclusive`가 True면 `[단독]` 추가 (제목에 이미 `[단독]`이 있으면 중복 제거)
- `scenario_b=True`일 때:
  - `action == "modified"`: `[수정]` 추가
  - `action == "added"`: `[신규]` 추가
- 복수 태그는 공백으로 결합 (예: `[단독] [신규]`)
- `_publisher_label()`로 언론사 표시: 복수 출처면 `[언론사 등 다수]`

메시지 구조:
```html
<b><a href="https://...">■ [단독] [신규] [한국일보] 검찰 수사 착수 (14:30)</a></b>
<blockquote expandable>검찰이 대규모 비리 사건에 대한 수사에 착수했다.
-> <i>자사 미보도 단독 기사</i>
<i>(이전 전달: 어제 브리핑에서 수사 착수 가능성 언급)</i></blockquote>
```

- `prev_reference`가 있으면 이전 전달 내역 표시 (`prev_ref` 필드)
- URL은 `item["url"]` 단일 값 사용 (check의 `article_urls` 리스트와 다름)
- `_truncate()`로 4096자 제한 적용

#### format_unchanged_report_items()

```python
def format_unchanged_report_items(items: list[dict]) -> list[str]:
```

시나리오 B에서 변경 없는 기존 항목들을 제목+링크로 모아 토글 메시지 목록으로 포맷팅한다. `_split_blockquote_messages()`로 4096자 이내 분할.

출력 형태:
```html
<b>기보고 5건</b>
<blockquote expandable>- <a href="...">[한국일보] 기사 제목 1 (14:30)</a>
- <a href="...">[조선일보] 기사 제목 2 (15:00)</a></blockquote>
```

---

## 4. scheduler.py 상세 해부

파일: `src/bot/scheduler.py`

자동 실행 스케줄 관리. `/schedule` 커맨드 핸들러, JobQueue 등록/해제, 자동 실행 콜백, 서버 재시작 복원을 담당한다.

### 4.1 /set_schedule 핸들러

스케줄 관리는 `src/bot/settings.py`의 `build_settings_handler()`에서 `/set_schedule` ConversationHandler로 처리된다.
2단계 대화: (1) 현재 스케줄 표시 + 입력 안내 (2) 사용자 입력 파싱 + 저장.

파싱: `[command] [time1] [time2] ...` 또는 `off`

**인자 없음** (`/schedule`): 현재 설정 표시

```
현재 자동 실행 설정:
  check: 09:00, 12:00, 15:00
  report: 08:30

/schedule off -- 전체 해제
```

등록된 스케줄이 없으면 사용법 안내 메시지 출력.

**`/schedule off`**: `repo.delete_all_schedules()` + `unregister_jobs()`로 전체 해제

**`/schedule check HH:MM ...` 또는 `/schedule report HH:MM ...`**:

1. command가 `"check"` 또는 `"report"`인지 검증
2. 시각 인자가 없으면 사용법 안내
3. 실행 횟수 제한 검증:
   ```python
   _MAX_TIMES = {"check": 30, "report": 30}
   ```
   - check: 최대 30개/일, report: 최대 30개/일
4. 시각 형식 검증:
   ```python
   _TIME_RE = re.compile(r"^\d{2}:\d{2}$")
   ```
   - 정규식 매칭 후 시/분 범위 검증 (0-23시, 0-59분)
5. `repo.save_schedules()`로 DB 저장
6. `unregister_jobs()`로 해당 command의 기존 잡 제거
7. `register_job()`으로 각 시각별 잡 재등록
8. 확인 메시지: `f"자동 {command} 설정 완료!\n매일 {times_str}에 자동 실행됩니다."`

### 4.2 JobQueue 관리

#### register_job()

```python
def register_job(
    app: Application,
    command: str,
    journalist_id: int,
    telegram_id: str,
    time_kst_str: str,
) -> None:
```

- `command`에 따라 콜백 결정: `"check"` -> `scheduled_check`, `"report"` -> `scheduled_report`
- KST 시각 문자열을 `time(hour=h, minute=m, tzinfo=_KST)` 객체로 변환
  - python-telegram-bot의 JobQueue가 tzinfo 기반으로 UTC 변환을 자동 처리
- 잡 이름 규칙: `"{command}_{journalist_id}_{time_kst_str}"` (예: `"check_1_09:00"`)
- `app.job_queue.run_daily()`로 매일 반복 실행 등록
- `data={"journalist_id": journalist_id}`로 콜백에 journalist_id 전달

#### unregister_jobs()

```python
def unregister_jobs(
    app: Application,
    journalist_id: int,
    command: str | None = None,
) -> None:
```

- `command`가 지정되면 해당 command만 제거, `None`이면 check + report 전체 제거
- 잡 이름 prefix 매칭으로 대상 잡 식별: `f"{command}_{journalist_id}_"`
- `job.schedule_removal()`로 제거 예약

### 4.3 자동 실행 콜백

#### scheduled_check()

```python
async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
```

JobQueue에서 호출되는 자동 check 실행. `check_handler()`와 동일한 파이프라인을 사용하되, `update` 대신 `context.job`에서 chat_id, journalist_id를 추출한다.

차이점:
- 메시지 전송에 `context.bot.send_message(chat_id=chat_id, ...)` 사용
- `async with lock:` -- 핸들러의 `lock.locked()` 체크와 달리, 스케줄러는 Lock을 기다린다 (순차 처리)
- 실행 시작 시 구분선 + 이모지 메시지 전송:
  ```
  ━━━━━━━━━━━━━━━━━━━━
  ⏰ 자동 타사체크
  ```
- 에러 시 `"[자동 체크] 실패: {format_error_message(e)}"` 메시지 전송

`_user_locks`, `_pipeline_semaphore`, `_run_check_pipeline()` 등을 `src/bot/handlers.py`에서 직접 임포트하여 사용한다.

#### scheduled_report()

```python
async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
```

`report_handler()`와 동일한 시나리오 A/B 분기 로직을 수행한다. `_handle_report_scenario_a()`, `_handle_report_scenario_b()` 함수를 handlers.py에서 임포트하여 그대로 사용한다.

자동 실행 시에도 당일 캐시 존재 여부에 따라 시나리오가 분기되므로, 오전에 수동 /report 실행 후 오후에 자동 실행되면 시나리오 B로 동작한다.

### 4.4 서버 재시작 복원 -- restore_schedules()

```python
async def restore_schedules(app: Application, db) -> None:
```

- `repo.get_all_schedules(db)`로 DB에 저장된 전체 스케줄 로드
- 각 스케줄에 대해 `register_job()` 호출하여 JobQueue에 재등록
- 로드된 스케줄 정보: `command`, `journalist_id`, `telegram_id`, `time_kst`
- 복원 완료 시 `logger.info("스케줄 복원 완료: %d건", len(schedules))` 로깅
- `main.py`의 `post_init()`에서 앱 시작 시 자동 호출

---

## 5. main.py 상세 해부

파일: `main.py`

애플리케이션 진입점. Bot 빌드, 핸들러 등록, 라이프사이클 관리를 담당한다.

### 5.1 Langfuse 설정

```python
load_dotenv()
if os.environ.get("LANGFUSE_PUBLIC_KEY"):
    from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
    from langfuse import get_client

    AnthropicInstrumentor().instrument()
    get_client()
```

- `.env`에서 환경변수 로드 후, `LANGFUSE_PUBLIC_KEY`가 설정되어 있을 때만 활성화
- OpenTelemetry 기반으로 Anthropic SDK 호출을 자동 계측(instrument)
- `get_client()`로 Langfuse 클라이언트 초기화
- 모든 Claude API 호출이 자동으로 Langfuse에 트레이싱됨
- 환경변수가 없으면 트레이싱 없이 정상 동작

### 5.1b post_init() -- 봇 명령어 메뉴 등록

`post_init()`에서 `application.bot.set_my_commands()`로 텔레그램 봇 명령어 메뉴를 등록한다:
- `/check` -- 키워드 기반 타사 체크
- `/report` -- 부서 주요 뉴스 브리핑
- `/status` -- 현재 설정 조회
- `/set_schedule` -- 자동 실행 예약 설정
- `/set_division` -- 부서 변경
- `/set_keyword` -- 모니터링 키워드 변경
- `/set_apikey` -- Claude API 키 변경

### 5.2 main() 함수

```python
def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
```

- `concurrent_updates(True)`: 여러 사용자의 업데이트를 동시에 처리. 기본값(False)이면 직렬 처리되어 한 사용자의 파이프라인 실행 중 다른 사용자의 요청이 대기하게 됨
- `post_init(post_init)`: 폴링 시작 전 초기화 콜백
- `post_shutdown(post_shutdown)`: 종료 시 정리 콜백

핸들러 등록 순서:

1. `build_conversation_handler()` -- `/start` 프로필 등록 (ConversationHandler)
2. `CommandHandler("check", check_handler)` -- `/check`
3. `CommandHandler("report", report_handler)` -- `/report`
4. `build_settings_handler()` -- `/set_keyword`, `/set_apikey`, `/set_schedule` (ConversationHandler)
5. `CommandHandler("set_division", set_division_handler)` -- `/set_division`
6. `CallbackQueryHandler(set_division_callback, pattern="^setdiv:")` -- 부서 변경 콜백
7. `CommandHandler("status", status_handler)` -- `/status` 현재 설정 조회
8. `CommandHandler("stats", stats_handler)` -- `/stats`

ConversationHandler가 가장 먼저 등록되므로, `/start` 대화 진행 중에는 다른 커맨드 핸들러보다 ConversationHandler가 우선 처리된다.

마지막에 `app.run_polling()`으로 Telegram 폴링 시작.

### 5.3 post_init()

```python
async def post_init(application: Application) -> None:
```

앱 시작 시 실행되는 초기화 콜백:

1. **DB 초기화**: `init_db(DB_PATH)` -- aiosqlite 연결 생성 + 테이블 생성. 반환된 db 객체를 `application.bot_data["db"]`에 저장하여 모든 핸들러에서 접근 가능
2. **캐시 정리**: `cleanup_old_data(db)` -- 오래된 캐시 데이터 삭제 (보관 기간: `CACHE_RETENTION_DAYS` = 5일)
3. **스케줄 복원**: `restore_schedules(application, db)` -- DB의 스케줄을 JobQueue에 재등록
4. **일일 정리 잡 등록**:
   ```python
   _KST = timezone(timedelta(hours=9))
   application.job_queue.run_daily(
       _daily_cleanup, time=time(hour=4, minute=0, tzinfo=_KST), name="daily_cleanup",
   )
   ```
   매일 04:00 KST에 `_daily_cleanup` 콜백 실행. 잡 이름은 `"daily_cleanup"`.

### 5.4 _daily_cleanup()

```python
async def _daily_cleanup(context) -> None:
```

`context.bot_data["db"]`에서 db 객체를 가져와 `cleanup_old_data(db)` 실행. 완료 시 `"일일 캐시 정리 완료"` 로깅.

### 5.5 post_shutdown()

```python
async def post_shutdown(application: Application) -> None:
```

앱 종료 시 `db.close()`로 aiosqlite 연결을 정리한다. `"DB 연결 종료"` 로깅.
