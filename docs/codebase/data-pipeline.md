# 데이터 파이프라인 상세

## 1. /check 파이프라인

`src/bot/handlers.py`의 `check_handler()` → `_run_check_pipeline()` 으로 구성된다.

### 1-1. 사용자 프로필 로드

```
check_handler() →  repo.get_journalist(db, telegram_id)
```

`src/storage/repository.py`의 `get_journalist()`가 `journalists` 테이블에서 telegram_id로 조회한다. 반환 dict 구조:

```python
{
    "id": int,
    "telegram_id": str,
    "department": str,           # 예: "사회부"
    "keywords": list[str],       # JSON 파싱된 리스트. 예: ["서부지검", "서부지법"]
    "api_key": str,              # Fernet 복호화된 원본 키
    "last_check_at": str | None, # UTC ISO 문자열 또는 None
    "created_at": str,
}
```

API 키는 `decrypt_api_key()`로 Fernet 복호화하여 평문으로 반환한다. 프로필이 없으면 None을 반환하고, 핸들러는 `/start로 등록해주세요` 메시지를 보낸 뒤 종료한다.

### 1-2. 동시 실행 방지

핸들러 진입 시 `_user_locks` dict에서 telegram_id별 `asyncio.Lock`을 가져온다. `lock.locked()`가 True이면 "이전 요청이 처리 중" 메시지를 보내고 즉시 반환한다.

파이프라인 자체는 `_pipeline_semaphore = asyncio.Semaphore(5)`로 전역 동시 실행을 5개로 제한한다. 1GB RAM 서버의 OOM을 방지하기 위한 설계이다.

### 1-3. 시간 윈도우 계산

`_run_check_pipeline()` 진입 후 첫 번째 작업이다.

```python
now = datetime.now(UTC)
last_check = journalist["last_check_at"]
if last_check:
    last_dt = datetime.fromisoformat(last_check).replace(tzinfo=UTC)
    window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
else:
    window_seconds = CHECK_MAX_WINDOW_SECONDS
since = now - timedelta(seconds=window_seconds)
```

- `CHECK_MAX_WINDOW_SECONDS`는 `src/config.py`에서 `3 * 60 * 60` (3시간)으로 설정
- `last_check_at`이 있으면 마지막 체크 시점부터 현재까지, 최대 3시간
- `last_check_at`이 None(첫 사용)이면 고정 3시간
- 파이프라인 완료 후 `repo.update_last_check_at(db, journalist["id"])`로 갱신

### 1-4. 네이버 뉴스 검색

```python
raw_articles = await search_news(journalist["keywords"], since)
```

`src/tools/search.py`의 `search_news()` 함수를 호출한다.

**동작 상세:**
- 키워드별로 `_search_keyword()` 호출. 키워드당 최대 2페이지(`_MAX_PAGES`) x 100건(`_DISPLAY`) = 200건 수집
- 키워드를 3개씩 묶어(`_BATCH_SIZE`) 동시 요청, 배치 간 0.5초 대기(`_BATCH_DELAY`)
- 429 응답 시 최대 2회 재시도(`_RETRY_MAX`), 재시도 간격 1초씩 증가
- `pubDate >= since` 조건으로 시간 윈도우 내 기사만 수집
- `originallink` 기준 URL 중복 제거 후 최신순 정렬
- `max_results` 기본값 200(`_MAX_TOTAL_RESULTS`)으로 상한

**반환 데이터 구조** (`_parse_item()` 참조):

```python
{
    "title": str,          # HTML 태그 제거된 제목
    "link": str,           # 네이버 뉴스 URL (n.news.naver.com)
    "originallink": str,   # 언론사 원문 URL
    "description": str,    # HTML 태그 제거된 요약
    "pubDate": datetime,   # RFC 2822 파싱된 datetime 객체
}
```

### 1-5. 언론사 필터

```python
filtered = filter_by_publisher(raw_articles)
```

`src/filters/publisher.py`의 `filter_by_publisher()` 함수를 호출한다.

- `data/publishers.json`의 화이트리스트를 `load_publishers()`로 로드 (lru_cache로 캐싱)
- 각 기사의 `originallink`에서 도메인을 추출하여 화이트리스트와 대조
- 서브도메인 매칭 지원: `news.chosun.com`은 `chosun.com`에 매칭
- 화이트리스트에 없는 언론사의 기사는 제거

건수 변화 예시: 200건 → 약 80~150건 (군소 매체, 블로그 등 제거)

### 1-6. 제목 기반 필터링

```python
_SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}
filtered = [
    a for a in filtered
    if not any(tag in a.get("title", "") for tag in _SKIP_TITLE_TAGS)
]
```

사진/영상 등 분석 가치가 없는 기사를 제목 태그로 제거한다. 이 필터는 `_run_check_pipeline()` 내부에 인라인으로 구현되어 있다.

### 1-7. Haiku 사전 필터

```python
filtered = await filter_check_articles(journalist["api_key"], filtered, journalist["department"])
```

`src/agents/check_agent.py`의 `filter_check_articles()` 함수로 부서 관련성 기반 사전 필터링을 수행한다. 제목+description만으로 판단하여 키워드 무관 기사, 사진 캡션, 중복 사안을 제거한다. 본문 스크래핑 전 단계이므로 스크래핑 비용을 절감한다.

**모델:** `claude-haiku-4-5-20251001` (temperature 0.0, max_tokens 2048)

### 1-8. 본문 스크래핑

```python
urls = [a["link"] for a in filtered]
bodies = await fetch_articles_batch(urls)
```

`src/tools/scraper.py`의 `fetch_articles_batch()` 함수를 호출한다.

**동작 상세:**
- `link` (네이버 뉴스 URL)를 대상으로 스크래핑
- 전역 세마포어 `_scrape_semaphore = asyncio.Semaphore(50)`으로 동시 요청 제한
- httpx.AsyncClient를 공유하여 연결 재사용, 타임아웃 10초
- `_parse_article_body()`로 HTML에서 본문 추출

**본문 추출 로직** (`_parse_article_body()`):
1. `article#dic_area` 또는 `div#newsct_article` 컨테이너 탐색
2. `<p>` 태그에서 문단 추출 (사진/이미지 래퍼 내 캡션 제외)
3. 소제목 판별: `_SUBHEADING_MARKERS` 특수문자(▶, ■ 등)로 시작하거나, 전체가 볼드인 짧은(50자 미만) 텍스트
4. 총 글자 수가 800자(`_MAX_CHARS`)에 도달하면 수집 중단
5. `<p>` 태그가 없으면 컨테이너의 직접 텍스트를 줄바꿈 기준으로 분리하여 같은 규칙 적용

**반환값:** `dict[str, str | None]` — URL을 키, 본문 텍스트(또는 None)를 값으로 하는 딕셔너리

### 1-8. 분석용 데이터 조립

필터링된 기사에 언론사명과 본문을 합쳐 Claude 분석용 리스트를 만든다.

```python
articles_for_analysis = []
for a in filtered:
    publisher = get_publisher_name(a["originallink"]) or ""
    body = bodies.get(a["link"], "") or ""
    pub_date_str = a["pubDate"].strftime("%Y-%m-%d %H:%M") if hasattr(a["pubDate"], "strftime") else str(a["pubDate"])
    articles_for_analysis.append({
        "title": a["title"],
        "publisher": publisher,    # 화이트리스트에서 조회한 언론사명
        "body": body,              # 스크래핑한 본문 3문단
        "url": a["link"],          # 네이버 뉴스 URL
        "pubDate": pub_date_str,   # "YYYY-MM-DD HH:MM" 형식
    })
```

`get_publisher_name()`은 `originallink` 도메인으로 화이트리스트에서 언론사명을 조회한다.

### 1-9. 과거 이력 로드

```python
history = await repo.get_recent_reported_articles(db, journalist["id"], hours=72)
```

`reported_articles` 테이블에서 최근 72시간 이력을 조회한다. 반환 구조:

```python
{
    "id": int,
    "checked_at": str,         # UTC ISO 문자열
    "topic_cluster": str,      # 주제 식별자
    "key_facts": list[str],    # JSON 파싱된 핵심 팩트 배열
    "summary": str,
    "article_urls": list[str], # JSON 파싱된 URL 배열
    "category": str,           # "exclusive" / "important" / "skip"
    "reason": str,             # 판단 근거 또는 스킵 사유
}
```

이 이력은 LLM에게 "이전 보고 이력"과 "이전 skip 이력"으로 분리되어 전달된다.

### 1-10. Claude 분석

```python
results = await analyze_articles(
    api_key=journalist["api_key"],
    articles=articles_for_analysis,
    history=history,
    department=journalist["department"],
    keywords=journalist["keywords"],
)
```

`src/agents/check_agent.py`의 `analyze_articles()` 함수를 호출한다.

**모델:** `claude-haiku-4-5-20251001` (temperature 0.0~0.4, max_tokens 16384, 5회 재시도)

**시스템 프롬프트 구성** (`_build_system_prompt()`):
- `DEPARTMENT_PROFILES`에서 부서별 `coverage`, `criteria`를 주입
- 기자의 키워드 목록을 포함
- 키워드 관련성 필터, 주요 기사 판단 기준, 단독 식별, 중복 제거, skip 승격 제한, 요약 작성 기준 등의 지시를 포함

**사용자 프롬프트 구성** (`_build_user_prompt()`):
- `[기자의 최근 보고 이력]`: 보고 이력 (category != "skip")을 시각 + topic_cluster + 핵심 팩트로 제공
- `[이전 skip 이력]`: skip 이력을 topic_cluster + reason으로 제공
- `[새로 수집된 기사]`: 번호 + [언론사] + 제목 + 본문 + 시각

**LLM 호출 방식:** tool_use 강제 (`tool_choice: {"type": "tool", "name": "submit_analysis"}`)

**`submit_analysis` 도구 스키마:**
- `thinking`: 기사별 판단 과정 (step별 pass/skip 기록)
- `results` 배열: `{category, topic_cluster, source_indices, merged_indices, title, summary, reason}`
  - category: `"exclusive"` (단독) 또는 `"important"` (주요)
- `skipped` 배열: `{topic_cluster, source_indices, title, reason}`

**응답 후처리:**
- `raw_results`와 `raw_skipped`를 병합. skipped 항목에는 `category: "skip"`을 주입
- 합쳐진 리스트를 반환

### 1-11. source_indices → URL 역매핑

`_run_check_pipeline()` 내에서 `_map_results_to_articles()` 헬퍼를 호출하여 LLM 결과에 원본 기사 정보를 매핑한다.

```python
_map_results_to_articles(results, articles_for_analysis, url_key="url")
```

`_map_results_to_articles()` 헬퍼는 다음 순서로 매칭한다:

1. **title 기반 매칭 (우선)**: `_match_article()`로 LLM이 반환한 제목을 원본 기사와 매칭. 정확 일치 → 정규화 후 일치 ([단독] 등 태그 제거) → substring 포함 (15자 이상) 순서로 시도
2. **source_indices 폴백**: title 매칭 실패 시 1-based 인덱스로 원본 기사 참조. 이 경우 title은 LLM 반환값을 유지하여 summary와의 일관성을 보장
3. 매칭된 기사에서 `url`, `publisher`, `pub_time`, `source_count`를 주입

### 1-12. 결과 저장

```python
await repo.save_reported_articles(db, journalist["id"], results)
```

`reported_articles` 테이블에 전체 결과(주요 + skip)를 저장한다. 각 항목의 `topic_cluster`, `key_facts`, `summary`, `article_urls`, `category`, `reason`을 INSERT한다.

### 1-13. 포맷팅 및 전송

`check_handler()`에서 결과를 `reported`(category != "skip")와 `skipped`(category == "skip")로 분리한 뒤:

1. **헤더:** `format_check_header(total, important, since, now)` — 검색 범위와 건수 요약
2. **주요 기사:** `pub_time` 역순(최신 먼저)으로 정렬 후, 기사 1건당 `format_article_message()` 호출하여 개별 메시지로 전송
3. **스킵 기사:** `format_skipped_articles()`로 제목+사유를 모아 하나의 접을 수 있는 blockquote 메시지로 전송

`format_article_message()` 출력 형태:
```
<b>[단독] [한국일보] 기사 제목 (15:30)</b>

요약 2~3문장

-> 판단 근거

기사 원문 (하이퍼링크)
```

### 1-14. 전체 건수 변화 예시

| 단계 | 함수 | 예상 건수 |
|------|------|----------|
| 네이버 검색 | `search_news()` | 최대 200건 |
| 언론사 필터 | `filter_by_publisher()` | ~80-150건 |
| 제목 필터 | 인라인 | ~70-140건 |
| Claude 분석 | `analyze_articles()` | 주요 3-10건 + skip 나머지 |
| 사용자 전송 | - | 주요만 개별, skip은 1건 |

---

## 2. /report 파이프라인

`src/bot/handlers.py`의 `report_handler()` → `_run_report_pipeline()` 으로 구성된다.

### 2-1. 사용자 프로필 로드

/check와 동일하게 `repo.get_journalist(db, telegram_id)` 호출.

### 2-2. 시나리오 판별

```python
today = datetime.now(_KST).strftime("%Y-%m-%d")
cache_id, is_new = await repo.get_or_create_report_cache(db, journalist["id"], today)

existing_items = []
if not is_new:
    existing_items = await repo.get_report_items_by_cache(db, cache_id)

is_scenario_a = is_new or len(existing_items) == 0
```

- `report_cache` 테이블에서 `(journalist_id, date)` 조합으로 조회
- 캐시가 없으면 새로 생성 (`is_new=True`)
- **시나리오 A:** `is_new`이거나 `existing_items`가 비어있을 때 → 당일 첫 생성
- **시나리오 B:** 캐시가 존재하고 `existing_items`가 있을 때 → 업데이트

날짜 기준은 KST이다. `datetime.now(_KST).strftime("%Y-%m-%d")`.

### 2-3. 시간 윈도우

```python
now = datetime.now(UTC)
since = now - timedelta(seconds=REPORT_MAX_WINDOW_SECONDS)
```

/report도 적응형 시간 윈도우를 사용한다. `last_report_at`이 있으면 마지막 리포트 시점부터 현재까지, 최대 `REPORT_MAX_WINDOW_SECONDS` (3시간). `last_report_at`이 None(첫 사용)이면 고정 3시간.

### 2-4. 부서 키워드로 네이버 검색

```python
profile = DEPARTMENT_PROFILES.get(dept_label, {})
report_keywords = profile.get("report_keywords", [])
raw_articles = await search_news(report_keywords, since, max_results=300)
```

`src/config.py`의 `DEPARTMENT_PROFILES`에서 부서별 `report_keywords`를 가져온다. 예를 들어 사회부는:

```python
"report_keywords": [
    "경찰 수사", "검찰 기소", "법원 판결", "사건사고",
    "재난 안전", "교육 정책", "노동 노사",
    "부동산 정책", "의료 보건", "복지 정책",
    "인권 차별", "환경 기후", "저출생 고령화",
]
```

/check는 기자 개인의 `keywords`(좁은 범위), /report는 부서 전체 `report_keywords`(넓은 범위)를 사용한다. `max_results`는 check와 동일하게 300이다.

### 2-5. 언론사 필터

```python
filtered = filter_by_publisher(raw_articles)
```

/check와 동일한 `src/filters/publisher.py`의 `filter_by_publisher()` 사용.

### 2-6. Haiku LLM 필터

```python
filtered = await filter_articles(journalist["api_key"], filtered, department)
```

`src/agents/report_agent.py`의 `filter_articles()` 함수를 호출한다. /report에만 있는 단계이며, 본문 스크래핑 전에 제목+description만으로 부서 무관 기사를 제거하여 스크래핑 비용을 절감한다.

**모델:** `claude-haiku-4-5-20251001` (temperature 0.0, max_tokens 2048)

**입력 구성:** 기사 목록을 번호+언론사+제목+description 형태로 조립:
```
[1] 한국일보 | 기사 제목 | 기사 요약
[2] 조선일보 | ...
```

**필터 기준 (시스템 프롬프트):**
1. 부서 관련성: 해당 부서 취재 영역에 해당하는 기사만 포함
2. 사진 캡션 제외: 본문 없이 사진 설명만 있는 포토뉴스 제외
3. 중복 사안 정리: 같은 사안의 다수 기사 중 대표 기사(최대 3건)만 선별
4. 애매한 경우 포함 쪽으로 판단

**LLM 호출 방식:** tool_use 강제 (`tool_choice: {"type": "tool", "name": "filter_news"}`)

**`filter_news` 도구 스키마:** `selected_indices` (int 배열) — 1-based 기사 번호

**후처리:** 1-based 인덱스를 0-based로 변환하여 원본 기사 리스트에서 추출. tool_use 응답이 없으면 전체 기사를 그대로 반환한다 (fallback).

건수 변화 예시: 300건 → 약 30-80건

### 2-7. 본문 스크래핑

```python
urls = [a["link"] for a in filtered]
bodies = await fetch_articles_batch(urls)
```

/check와 동일. LLM 필터 후 줄어든 건수만 스크래핑하므로 비용 효율적이다.

### 2-8. 분석용 데이터 조립

```python
articles_for_analysis.append({
    "title": a["title"],
    "publisher": publisher,
    "body": body,
    "originallink": a["originallink"],  # /check와 차이: originallink 포함
    "link": a["link"],                  # /check와 차이: link 포함
    "pubDate": pub_date_str,
})
```

/check의 조립 데이터와 비교하면, /report는 `originallink`과 `link`를 모두 포함한다. /check는 `url`(= link)만 포함한다.

### 2-9. 과거 리포트 이력 로드

```python
report_history = await repo.get_recent_report_items(db, journalist["id"])
```

`report_items` 테이블에서 최근 2일(`days=2`)간 이력을 조회한다. 반환 구조:

```python
{
    "title": str,
    "summary": str,
    "category": str,       # "follow_up" / "new"
    "key_facts": list[str], # JSON 파싱된 핵심 팩트 배열
    "created_at": str,
}
```

이 이력은 LLM에게 `[이전 보고 이력 - 최근 2일]`로 전달되어 follow_up/new 분류의 근거가 된다.

### 2-10. 기존 캐시 로드 (시나리오 B)

시나리오 B일 때 `existing_items`가 파이프라인에 전달된다.

```python
results = await _run_report_pipeline(
    db, journalist,
    existing_items=existing_items if not is_scenario_a else None,
)
```

시나리오 A이면 `existing_items=None`, 시나리오 B이면 `existing_items=기존 항목 리스트`가 전달된다.

### 2-11. Claude 분석

```python
results = await analyze_report_articles(
    api_key=journalist["api_key"],
    articles=articles_for_analysis,
    report_history=report_history,
    existing_items=existing_items,
    department=department,
)
```

`src/agents/report_agent.py`의 `analyze_report_articles()` 함수를 호출한다.

**모델:** `claude-haiku-4-5-20251001` (temperature 0.0~0.4, max_tokens 16384, 5회 재시도)

**시스템 프롬프트 구성** (`_build_system_prompt()`):
- 부서별 취재 영역, 판단 기준, 단독 식별 기준, 제외 기준, 요약 작성 기준을 포함
- 시나리오 A/B에 따라 출력 규칙이 달라짐 (3장에서 상세 설명)

**사용자 프롬프트 구성** (`_build_user_prompt()`):
- `[이전 보고 이력 - 최근 2일]`: title, summary, key_facts, category, created_at
- 시나리오 B일 때 `[오늘 기존 항목]`: 항목순번, title, summary, key_facts
- `[수집된 기사 목록]`: 번호 + [언론사] + 제목 + 본문 + 시각

**LLM 호출 방식:** tool_use 강제 (`tool_choice: {"type": "tool", "name": "submit_report"}`)

**`submit_report` 도구 스키마:**
- `thinking`: 기사별 판단 과정 (step별 pass/skip 기록)
- `results` 배열: `{title, source_indices, merged_indices, summary, reason, exclusive}` (action/item_id는 시나리오 B 전용 추가)
  - action: `"modified"` (기존 수정) / `"added"` (신규) — 시나리오 B 전용
  - `category`, `prev_reference` 필드는 스키마에서 제거됨. `_parse_report_response()`에서 기본값 주입 (`category: "new"`, `prev_reference: ""`)

### 2-12. source_indices → URL 역매핑

```python
_map_results_to_articles(results, articles_for_analysis, url_key="link")
```

`_map_results_to_articles()` 헬퍼로 check와 동일한 매칭 로직을 사용한다 (title 기반 우선, source_indices 폴백). report는 `url_key="link"`로 네이버 뉴스 URL을 사용한다.

### 2-13. 결과 저장/갱신 및 전송

시나리오에 따라 `_handle_report_scenario_a()` 또는 `_handle_report_scenario_b()`를 호출한다.

**시나리오 A** (`_handle_report_scenario_a()`):
1. `repo.save_report_items(db, cache_id, results)` — 전체 항목을 report_items에 INSERT
2. `pub_time` 역순으로 정렬
3. `format_report_header_a()` 헤더 전송
4. 항목별 `format_report_item()` 호출하여 개별 메시지 전송

**시나리오 B** (`_handle_report_scenario_b()`): 4장에서 상세 설명.

---

## 3. 시나리오 A/B 분기 로직

### 3-1. 판별 기준

| 조건 | 시나리오 |
|------|---------|
| 당일(KST) `report_cache`가 존재하지 않음 | A |
| `report_cache`는 있으나 `report_items`가 0건 | A |
| `report_cache`와 `report_items`가 모두 존재 | B |

```python
is_scenario_a = is_new or len(existing_items) == 0
```

### 3-2. LLM에 전달되는 데이터 차이

**시나리오 A (첫 생성):**
- 시스템 프롬프트: `[출력 규칙 - 첫 생성]` 섹션 포함
  - 건수보다 품질 우선, 기준 미달 시 적게 선정 가능
  - follow_up/new 분류, source_indices 기재, exclusive 판별
- 사용자 프롬프트: 기존 캐시 섹션 없음
- `existing_items=None`으로 전달

**시나리오 B (업데이트):**
- 시스템 프롬프트에 `[오늘 기존 캐시]` 섹션 추가 (id, title, summary, tags)
- 시스템 프롬프트: `[출력 규칙 - 업데이트]` 섹션 포함
  - 기존 캐시와 비교하여 변경/추가만 보고
  - modified는 item_id 필수
  - 변경 없으면 빈 배열
  - 추가 항목은 적극적으로 탐색
- 사용자 프롬프트에도 `[오늘 기존 캐시 항목]` 섹션 포함
- `existing_items=기존 항목 리스트`로 전달

### 3-3. 시나리오 B의 결과 처리 (_handle_report_scenario_b)

LLM이 반환한 `delta_results`에 대해:

1. **action 보정:** action 필드가 없는 항목에 `"added"` 기본값 부여
2. **modified 항목 매핑:** `item_id`로 기존 항목과 대조
3. **merged_items 리스트 조립:**
   - 기존 항목 중 수정된 것: 새 summary, reason, exclusive, key_facts로 갱신 + `action: "modified"`
   - 기존 항목 중 변경 없는 것: `action: "unchanged"`
   - 신규 추가: `action: "added"`
4. **DB 반영:**
   - 추가 항목: `repo.save_report_items()`
   - 수정 항목: `repo.update_report_item()` (summary, reason, exclusive, key_facts 갱신)
5. **정렬 및 전송:**
   - 2단계 안정 정렬: 1) pub_time 역순 → 2) action 그룹 순 (modified/added 먼저, unchanged 뒤)
   - `format_report_header_b()`: 총 건수, 수정 N건, 추가 N건 표시
   - `format_report_item(item, scenario_b=True)`: [수정], [신규] 태그 포함

`format_report_item()` 시나리오 B 출력 형태:
```
<b>[수정] [한국일보] 기사 제목 (15:30)</b>

요약 2~3줄

-> 선택 사유

(이전 전달: 2025-01-15 "이전 제목")

기사 원문 (하이퍼링크)
```

---

## 4. 데이터 변환 형태 상세

### 4-1. /check 데이터 흐름

```
search_news() 반환
─────────────────
[
  {"title": str, "link": str, "originallink": str, "description": str, "pubDate": datetime},
  ...
]
        │
        ▼  filter_by_publisher()
[같은 구조, 화이트리스트 매칭만 남음]
        │
        ▼  제목 태그 필터 (인라인)
[같은 구조, [포토] 등 제거]
        │
        ▼  fetch_articles_batch(urls) → dict[str, str|None]
{
  "https://n.news.naver.com/...": "본문 1문단\n본문 2문단\n본문 3문단",
  "https://n.news.naver.com/...": None,  # 추출 실패
}
        │
        ▼  분석용 데이터 조립
[
  {"title": str, "publisher": str, "body": str, "url": str, "pubDate": "YYYY-MM-DD HH:MM"},
  ...
]
        │
        ▼  analyze_articles() → Claude tool_use
[
  {
    "category": "exclusive",
    "topic_cluster": "서부지검 특수부 인사",
    "source_indices": [3],
    "merged_indices": [5, 7],
    "title": "기사 제목",
    "summary": "요약 2~3문장",
    "reason": "판단 근거",
  },
  {
    "category": "skip",
    "topic_cluster": "주제명",
    "source_indices": [1],
    "title": "기사 제목",
    "reason": "키워드 무관",
  },
]
        │
        ▼  URL 역매핑 (handlers.py)
[
  {
    "category": "exclusive",
    "topic_cluster": "...",
    "article_urls": ["https://n.news.naver.com/..."],
    "merged_from": ["https://n.news.naver.com/...", "https://n.news.naver.com/..."],
    "publisher": "한국일보",
    "pub_time": "15:30",
    "title": "...", "summary": "...", "reason": "...",
  },
  ...
]
        │
        ▼  save_reported_articles() → reported_articles 테이블
        │
        ▼  format_article_message() → Telegram HTML
```

### 4-2. /report 데이터 흐름

```
search_news(report_keywords, max_results=300)
        │
        ▼  filter_by_publisher()
        │
        ▼  filter_articles() ← Haiku LLM 필터
        │
        ▼  fetch_articles_batch(urls)
        │
        ▼  분석용 데이터 조립
[
  {"title": str, "publisher": str, "body": str, "originallink": str, "link": str, "pubDate": str},
  ...
]
        │
        ▼  analyze_report_articles() → Claude tool_use
[
  {
    "action": "added",          # 시나리오 B에서만 의미
    "item_id": 0,
    "title": "기사 제목",
    "source_indices": [2, 5],
    "merged_indices": [3],
    "summary": "요약 2~3줄",
    "reason": "선택 사유",
    "exclusive": false,
    "category": "new",           # _parse_report_response()에서 기본값 주입
    "prev_reference": "",        # _parse_report_response()에서 기본값 주입
  },
  ...
]
        │
        ▼  URL 역매핑 (handlers.py)
        │  url → src["link"] (네이버 뉴스 URL)
        │  publisher, pub_time 주입
        │
        ▼  시나리오 A: save_report_items() → report_items 테이블
           시나리오 B: save_report_items() (추가분) + update_report_item() (수정분)
        │
        ▼  format_report_item() → Telegram HTML
```

---

## 5. 중복 제거 메커니즘

### 5-1. /check의 중복 제거

**URL 기준 검색 단계 중복 제거 (search.py)**

`search_news()`에서 키워드별 결과를 병합할 때 `originallink`를 기준으로 중복을 제거한다.

```python
seen_urls: set[str] = set()
for keyword_results in all_results:
    for article in keyword_results:
        url = article["originallink"]
        if url not in seen_urls:
            seen_urls.add(url)
            results.append(article)
```

동일 기사가 여러 키워드에 걸릴 때 한 번만 포함한다.

**이력 기반 중복 제거 (LLM 판단)**

`reported_articles` 테이블의 72시간 이력을 LLM에게 전달한다. 이력에는 `topic_cluster`와 `key_facts`가 포함되어 있어 LLM이 실질적 내용 중복을 판단한다.

시스템 프롬프트의 중복 제거 기준:
1. 동일 배치 내: 같은 사안의 여러 언론사 기사 → 가장 포괄적인 1건만 남김 (나머지는 `merged_indices`에 기재)
2. 이전 보고 대비: 이력과 실질적으로 동일한 내용이면 skip
3. 중복 예외: 이전 보고 주제라도 중요한 새 팩트가 있으면 보고

**skip 이력 활용**

skip 판정된 기사도 `reported_articles`에 `category: "skip"`으로 저장된다. 다음 check 시 LLM에게 `[이전 skip 이력]`으로 전달되며, 시스템 프롬프트에서 다음과 같이 지시한다:

> 이전에 skip 판정된 주제와 동일한 기사는 원칙적으로 skip을 유지한다. skip 사유를 뒤집을 새로운 정보(공식 발표, 수사 진전, 복수 언론 보도 전환 등)가 있을 때만 승격한다.

### 5-2. /report의 중복 제거

**URL 기준 검색 단계 중복 제거**

/check와 동일하게 `search_news()`에서 `originallink` 기준으로 제거.

**Haiku LLM 필터 단계 중복 정리**

`filter_articles()`에서 Haiku에게 "같은 사안의 다수 기사 중 대표 기사(최대 3건)만 선별"을 지시한다. 본문 스크래핑 전에 제목+description만으로 대량의 유사 기사를 정리한다.

**이력 기반 중복 제거 (LLM 판단)**

`report_items`의 2일(`days=2`)간 이력을 `get_recent_report_items()`로 조회하여 LLM에게 전달한다. LLM은 이 이력을 참고해 `category: "follow_up"` (후속) 또는 `"new"` (신규)를 분류한다.

**시나리오 B 기존 캐시 기반 중복 제거**

당일 기존 report_items를 LLM에게 `[오늘 기존 캐시]`로 전달한다. LLM은 기존 항목과 실질적으로 동일한 기사는 출력하지 않고, 새 팩트가 있으면 `action: "modified"`로, 완전히 새로운 사안이면 `action: "added"`로 보고한다.

### 5-3. 캐시 보관 기간

- `reported_articles`: `cleanup_old_data()`에서 `CACHE_RETENTION_DAYS`(5일) 이상 지난 레코드 삭제
- `report_cache` / `report_items`: 동일하게 `CACHE_RETENTION_DAYS` 기준으로 삭제

[상세: storage.md]
