# Report 에이전트: web_search → Naver API 전환 계획

## 배경

현재 `/report`는 Claude 에이전트 루프(최대 20턴)로 `web_search`를 반복 호출한다.
매 턴마다 이전 대화 전체가 재전송되어 input 토큰이 기하급수적으로 누적된다.

`/check`는 Naver API → 필터 → 본문수집 → Claude 1회 호출 구조로 input ~24K tokens 수준이다.
report도 뉴스 수집을 Naver API로 전환하고 Claude를 2회 호출(필터 + 분석)하여 토큰 비용과 응답 시간을 대폭 절감한다.

## check와 report의 구조적 차이 (유지해야 할 것)

report는 check와 뉴스 수집 방식만 통일하되, 아래 고유 특성은 반드시 유지한다:

| 항목 | check | report |
|------|-------|--------|
| 시나리오 | 단일 (매번 새 분석) | **A/B 유지** — 당일 첫 생성 vs 업데이트 |
| 결과 구조 | results + skipped 이원 분리 | **results만** (skip 불필요, 선택된 것만 출력) |
| 선택 사유 | reason 필드 (주요 판단 근거) | **reason 필드** (`->` 형태로 선택 사유 표시) |
| 이력 참조 | 최근 72시간 check 이력 | **이틀치 report 이력** (후속 판단용) |
| 누적 방식 | 매번 독립 분석 | **누적 갱신** — 기존 항목 유지 + 갱신/추가 |
| 후속 판단 | 없음 | **follow_up/new 분류** (이전 report 기반) |

## 핵심 변경

| 항목 | 현재 (report) | 변경 후 |
|------|--------------|---------|
| 뉴스 수집 | Claude web_search (에이전트 루프) | Naver API (부서별 report_keywords) |
| 필터링 | Claude 자체 판단 | LLM 필터 (Haiku, 제목+description 기반) |
| Claude 분석 | 다중 턴 (10~20회) | 1회 (tool_use) |
| 본문 수집 | fetch_article (Claude 판단) | fetch_articles_batch (필터 통과 기사만) |
| input 토큰 | 수십만 (턴 누적) | ~30K (필터 + 분석 합산) |
| 시간 윈도우 | "당일 기사" (Claude 판단) | 최근 3시간 (check와 동일) |
| 시나리오 A/B | Claude가 프롬프트로 구분 | **파이프라인 레벨에서 구분** (유지) |
| 수집 상한 | N/A | 400건 (check 200건 대비 확대) |

## 영향 범위 분석

### 삭제 대상
- `report_agent.py`: web_search 에이전트 루프, fetch_article 도구, JSON 폴백 파싱, `_execute_custom_tools()`, `_MAX_AGENT_TURNS`, `_parse_response()`
- `formatters.py`: `format_report_references()`, `format_report_no_update()` (미사용)
- `repository.py`: `get_recent_report_tags()` (Haiku 필터 전환으로 미사용)

### 신규 대상
- `report_agent.py`: `filter_articles()` — Haiku 기반 기사 필터 함수
- `repository.py`: `get_recent_report_items()` — 이틀치 report 이력 조회 함수
- `models.py`: `report_items` 테이블에 `reason`, `exclusive` 컬럼 추가

### 수정 대상
- `report_agent.py`: 단일 Claude 분석 호출 구조로 재작성 (시나리오 A/B 프롬프트 분리는 유지)
- `handlers.py`: `report_handler`에 뉴스 수집 파이프라인 추가, 시나리오 A/B 핸들러 수정
- `scheduler.py`: `scheduled_report`에 파이프라인 적용, import 변경 (`run_report_agent` → `_run_report_pipeline`)
- `formatters.py`: `format_report_item()`에 reason → `->` 표시 추가
- `config.py`: 부서별 `report_keywords` 추가, `REPORT_MAX_WINDOW_SECONDS` 상수 추가
- `search.py`: `search_news()`에 `max_results` 파라미터 추가 (기본값 200, report는 400 전달)
- `repository.py`: `get_report_items_by_cache()`에 reason/exclusive 반환 추가, `update_report_item()` reason/exclusive/tags 갱신으로 확장, `save_report_items()` reason/exclusive 저장 추가

### 유지 대상
- `report_cache`, `report_items` 테이블 및 관련 저장소 함수 전체
- `_handle_report_scenario_a()`, `_handle_report_scenario_b()` (수정하되 유지)
- `format_report_header_a()`, `format_report_header_b()`, `format_report_item()` (수정하되 유지)
- `get_or_create_report_cache()`, `get_report_items_by_cache()`, `save_report_items()`, `update_report_item()` (수정하되 유지)
- `cleanup_old_data()`
- check 파이프라인 인프라: `search_news`, `filter_by_publisher`, `fetch_articles_batch`

---

## Phase 1: 부서별 report_keywords 정의 + config 확장

### 목표
각 부서의 coverage 영역을 커버하는 검색 키워드 세트를 정의한다.

### 작업
1. `src/config.py`의 `DEPARTMENT_PROFILES`에 `report_keywords` 필드 추가
2. 각 부서별 10~15개 키워드 정의 (coverage 기반)
   - 사회부: "경찰 수사", "검찰 기소", "법원 판결", "사건사고", "재난", "교육 정책", "노동", "부동산", "의료", "복지" 등
   - 정치부: "국회", "대통령실", "여당", "야당", "외교", "헌법재판소", "선거" 등
   - 경제부: "기준금리", "부동산 정책", "물가", "수출", "금융", "환율", "가계부채" 등
   - 산업부: "반도체", "배터리", "AI", "대기업 실적", "M&A", "스타트업", "에너지" 등
   - 문화부: "영화", "드라마", "방송", "출판", "관광", "게임", "웹툰", "OTT" 등
   - 스포츠부: "프로야구", "축구", "올림픽", "FA 이적", "체육 행정" 등
3. `REPORT_MAX_WINDOW_SECONDS` 상수 추가 (3시간 = 10800)
4. `search.py`의 `search_news()`에 `max_results` 파라미터 추가 (기본값 200, report는 400 전달)

### 검증
- 키워드 수와 Naver API 호출량 관계 확인 (키워드당 1 API 호출, 배치 3개씩)
- 15개 키워드 → 5배치 × 0.5초 = 2.5초 수집 시간

---

## Phase 1.5: LLM 필터 파이프라인 (Haiku)

### 목표
넓은 키워드로 수집된 대량의 기사를 LLM 필터(Haiku)로 부서 관련 기사만 선별한다.
본문 스크래핑 전에 필터링하여 불필요한 HTTP 요청과 메인 LLM 토큰을 절약한다.

### 배경
check는 좁은 키워드(기관명 등)라 수집 기사가 적지만, report는 부서 전체 키워드(15개)로
수집하므로 언론사 필터 후에도 34~104건이 남는다. 이 중에는:
- **크로스 오염**: 문화부에 정치 기사 유입 등 (네이버 검색 알고리즘의 확장 매칭)
- **사진 캡션**: "경기 펼치는 정대윤" 등 본문 없는 포토 뉴스
- **중복 사안**: 같은 사안을 다수 언론사가 보도

규칙 기반 필터(제목 길이, Jaccard 등)보다 LLM이 이 세 가지를 더 정확하게 판단할 수 있다.

### 파이프라인 (순서대로)
```
1. Naver API 수집 (키워드 15개, 3시간 윈도우, 최대 400건)
   → URL 중복제거 후 ~300~500건

2. 언론사 필터 (publishers.json 화이트리스트)
   → ~50~150건

3. LLM 필터 — Haiku (제목 + description만 사용, 본문 불필요)
   → 부서 관련성 판단 + 사진 캡션 제거 + 중복 사안 대표 기사 선별
   → ~30~60건

4. fetch_articles_batch — 필터 통과 기사만 본문 스크래핑
   → 스크래핑 대상이 줄어 HTTP 요청 절약

5. LLM 분석 — Claude 메인 (tool_use, temperature=0.0)
   → 주요 기사 선별, 요약, reason 작성
```

### LLM 필터 상세 (Phase 1.5 핵심)

**구현 위치**: `src/agents/report_agent.py`에 `filter_articles()` 함수 추가

**모델**: Haiku (저렴, 빠름)

**입력**: 언론사 필터 후 기사 목록 (제목 + description만, 본문 없음)
```
[1] 조선일보 | 검찰, 이재명 뇌물 혐의 기소 | 검찰이 이재명 대표를 뇌물 혐의로...
[2] 연합뉴스 | 경기 펼치는 정대윤 | 12일 이탈리아 리비뇨에서 열린...
[3] KBS | 이진숙 "대구시장 출마" | 이진숙 전 방통위원장이...
...
```

**프롬프트 지시사항**:
- 부서(예: 사회부)의 취재 영역(coverage) 제공
- 다음 기준으로 기사 번호를 선별:
  1. **부서 관련성**: 해당 부서 취재 영역에 해당하는 기사만 포함
  2. **사진 캡션 제외**: 본문 없이 사진 설명만 있는 기사 제외
  3. **중복 사안 정리**: 같은 사안의 다수 기사 중 대표 기사(최대 3건)만 선별
- 출력: 선별된 기사 번호 배열 (예: [1, 5, 8, 12, ...])

**출력 방식**: tool_use (번호 배열 반환) — 파싱 안정성 확보

**비용 추정**:
- 입력: 100건 × (제목 ~30자 + description ~100자) ≈ ~5K tokens
- 출력: 번호 배열 ≈ ~0.5K tokens
- Haiku 비용: 거의 무시 수준 (메인 분석의 1/10 이하)

**성능 추정**:
- Haiku 응답 시간: ~1~2초
- 스크래핑 절약: 60~100건 → 30~60건 (HTTP 요청 절반 감소)
- 메인 LLM 입력 절감: 노이즈 기사 제거로 ~10K tokens 감소

### 검증
- Haiku 필터가 부서 무관 기사를 정확히 제거하는지 (크로스 오염)
- 사진 캡션 기사가 걸러지는지
- 같은 사안의 중복 기사가 3건 이내로 정리되는지
- 정상 기사가 오탈되지 않는지 (false positive)

---

## Phase 2: DB 스키마 + 저장소 함수 확장

### 목표
report 결과에 reason, exclusive 필드를 저장하고, 이틀치 report 이력을 조회할 수 있게 한다.
Phase 3~4의 코드가 참조할 DB 구조를 먼저 확보한다.

### 작업

#### `models.py` — report_items 테이블 스키마 확장
- `reason TEXT` 컬럼 추가 (선택 사유)
- `exclusive INTEGER DEFAULT 0` 컬럼 추가 ([단독] 여부)
- 기존 데이터 호환: ALTER TABLE로 컬럼 추가 (기본값 있으므로 안전)

#### `repository.py` — 신규 함수
- `get_recent_report_items(db, journalist_id, days=2)` 신규
  - 최근 2일간의 report_items를 report_cache 기준으로 조회
  - follow_up/new 판단에 사용 (기존 `get_recent_report_tags()`는 태그만 반환하므로 불충분)
  - 반환 필드: title, summary, tags, category, created_at

#### `repository.py` — 기존 함수 수정
- `save_report_items()`: reason, exclusive 필드 저장 추가
- `get_report_items_by_cache()`: 반환 dict에 reason, exclusive 필드 추가
- `update_report_item()`: summary만 갱신 → reason, exclusive, tags도 함께 갱신하도록 확장

#### `repository.py` — 삭제
- `get_recent_report_tags()`: Haiku 필터 전환으로 미사용 → 삭제

### 검증
- 신규 컬럼이 기존 데이터와 호환되는지
- 이틀치 이력 조회가 정확한지
- reason, exclusive가 정상 저장/조회되는지
- `get_report_items_by_cache()` 반환값에 reason, exclusive 포함 확인
- `update_report_item()` 확장 필드 갱신 확인

---

## Phase 3: report_agent.py 재작성

### 목표
에이전트 루프를 제거하고, LLM 필터(Haiku) + LLM 분석(Claude 메인) 2회 호출 구조로 전환한다.
시나리오 A/B에 따라 다른 프롬프트를 사용하되, 호출 방식은 동일하게 tool_use.

### 삭제
- `TOOLS` 리스트 (web_search, fetch_article, submit_report 3개 도구 묶음)
- `_OUTPUT_INSTRUCTIONS_A`, `_OUTPUT_INSTRUCTIONS_B`
- `_parse_response()` (JSON 폴백 파싱)
- `_execute_custom_tools()` (fetch_article 실행)
- `run_report_agent()` 에이전트 루프 전체
- `_MAX_AGENT_TURNS` 상수

### 신규 작성

#### 1. LLM 필터 함수 (`filter_articles()`)
- Phase 1.5에서 정의한 Haiku 기반 필터
- 인자: api_key, articles (제목+description), department
- 반환: 선별된 기사 인덱스 리스트
- Langfuse 트레이싱: `@observe()` 데코레이터 적용

**Haiku tool_use 스키마** (`filter_news`):
```json
{
  "name": "filter_news",
  "description": "부서 관련 기사 번호를 선별합니다",
  "input_schema": {
    "type": "object",
    "properties": {
      "selected_indices": {
        "type": "array",
        "items": {"type": "integer"},
        "description": "선별된 기사 번호 배열"
      }
    },
    "required": ["selected_indices"]
  }
}
```

#### 2. 메인 분석 tool_use 스키마 (`submit_report`)
- results 배열만 사용 (skipped 배열 없음)
- 각 항목 필드:
  - `action`: "modified" (기존 항목 갱신) / "added" (신규 추가) — 시나리오 B 전용
  - `item_id`: 기존 항목 ID (action이 modified일 때만, 나머지 null) — 시나리오 B 전용
  - `title`: 기사 제목
  - `url`: 대표 기사 URL 1개 (수집된 기사 목록의 originallink)
  - `source_indices`: 참조 기사 번호 배열 (수집된 기사 목록 기준, URL 역매핑용)
  - `summary`: 2~3줄 구체적 요약
  - `reason`: 선택 사유 1문장 (왜 이 기사를 브리핑에 포함했는지)
  - `tags`: 주제 태그 배열
  - `category`: "follow_up" (이전 보도 후속) / "new" (신규)
  - `exclusive`: true/false ([단독] 여부)
  - `prev_reference`: follow_up이면 "YYYY-MM-DD \"이전 제목\"", new이면 null

#### 3. 시스템 프롬프트 (`_build_system_prompt`)

Haiku 필터가 부서 관련성과 중복을 사전 정리하므로, 메인 프롬프트는 **주요 기사 선별과 요약에 집중**한다.

프롬프트 구성:
1. **역할 정의**: "{dept_label} 데스크의 뉴스 브리핑 보조"
2. **취재 영역**: DEPARTMENT_PROFILES의 coverage (부서가 다루는 분야 범위)
3. **중요 기사 판단 기준**:
   - DEPARTMENT_PROFILES의 criteria를 1차 기준으로 사용
   - 추가 기준: 사회적 파장, 복수 언론 보도 여부, 후속 보도 가능성, 데스크가 주목할 사안
   - 제외 기준: 단발성 사건사고, 정례 발표, 단순 일정 안내, 사회적 관심 낮은 소규모 이슈
4. **요약 작성 기준**: 구체적 팩트 포함, 사실 기반
5. **출력 규칙**: 시나리오별 분기

시나리오 A (첫 생성):
- 수집된 기사 중 부서 데스크가 주목할 사안을 선별
- 이틀치 이전 report 이력을 참조하여 follow_up/new 분류
- 선택 사유(reason)를 명시
- 최소 8개, 10개 이상 목표

시나리오 B (업데이트):
- 기존 캐시 항목을 컨텍스트로 제공
- 새 기사 중 기존 항목에 추가할 팩트가 있으면 modified (요약 갱신)
- 기존에 없는 새 기사는 added
- 변경 없는 항목은 출력하지 않음

#### 4. 사용자 프롬프트 (`_build_user_prompt`)
- 수집된 기사 목록 (번호, 언론사, 제목, 본문)
- 이전 report 이력:
  - 시나리오 A: 이틀치 report_items (후속 판단용)
  - 시나리오 B: 이틀치 이력 + 당일 기존 캐시 항목
- 분석 지시

#### 5. 분석 함수 (`analyze_report_articles()`)
- Claude 1회 호출 (tool_use, temperature=0.0)
- 인자: api_key, articles, report_history, existing_items, department
- 시나리오 A: existing_items=None
- 시나리오 B: existing_items=기존 캐시 항목
- 결과: results 리스트 반환
- Langfuse 트레이싱: `@observe()` 데코레이터 적용

### 검증
- Haiku 필터 → 메인 분석 2단계 호출 정상 동작
- 시나리오 A/B별 올바른 프롬프트 생성
- tool_use 스키마 정합성
- Langfuse에서 필터/분석 각각의 토큰 사용량 확인 가능

---

## Phase 4: handlers.py report 파이프라인 구축

### 목표
report_handler에 Naver API 기반 뉴스 수집 파이프라인을 추가한다.
시나리오 A/B 핸들러는 유지하되, 뉴스 수집 부분만 교체한다.

### 신규: `_run_report_pipeline()`

**함수 시그니처**:
```python
async def _run_report_pipeline(
    department: str,
    existing_items: list[dict] | None = None,
) -> list[dict]:
```

check의 `_run_check_pipeline`과 유사한 구조:
1. 부서별 report_keywords로 `search_news()` 호출 (max_results=400)
2. `filter_by_publisher()` — 언론사 필터
3. `filter_articles()` — Haiku LLM 필터 (제목+description 기반)
4. `fetch_articles_batch()` — 필터 통과 기사만 본문 수집
5. 이틀치 이전 report 이력 로드 (`get_recent_report_items()`)
6. `analyze_report_articles()` — Claude 메인 분석 (tool_use)
7. 결과에 URL/언론사 주입

### 수정: `report_handler()`
- 기존: `run_report_agent()` 직접 호출
- 변경: `_run_report_pipeline()` 호출 후 시나리오 A/B 핸들러로 라우팅
- 시나리오 판단 로직은 기존과 동일 (cache 존재 여부)
- 시나리오 B 시 `existing_items`를 `_run_report_pipeline()`에 전달

### 수정: `_handle_report_scenario_a()`
- 기존 로직 대부분 유지
- 입력이 `run_report_agent()` 결과 → `_run_report_pipeline()` 결과로 변경

### 수정: `_handle_report_scenario_b()`
- 기존 누적 갱신 로직 유지 (modified/added/unchanged 병합)
- 입력이 `run_report_agent()` 결과 → `_run_report_pipeline()` 결과로 변경
- modified 항목 병합 시 reason, exclusive, tags도 함께 갱신 (`update_report_item()` 확장에 맞춤)

### 검증
- 시나리오 A: 첫 요청 시 전체 브리핑 생성
- 시나리오 B: 재요청 시 기존 항목 갱신/추가 정상 동작
- check_handler와 파이프라인 구조 일관성

---

## Phase 5: scheduler.py + formatters.py 수정

### 목표
예약 실행에 파이프라인을 적용하고, report 메시지에 선택 사유를 표시한다.

### 수정: `scheduled_report()` (scheduler.py)
- 기존: `run_report_agent()` 직접 호출 (import: `from src.agents.report_agent import run_report_agent`)
- 변경: `_run_report_pipeline()` 호출 (import: `from src.bot.handlers import _run_report_pipeline`)
- 시나리오 A/B 핸들러 호출은 기존과 동일

### 삭제 (formatters.py)
- `format_report_references()` — 미사용 함수
- `format_report_no_update()` — 미사용 함수

### 수정: `format_report_item()` (formatters.py)
- reason 필드 추가: check의 `format_article_message()`처럼 `-> <i>reason</i>` 형태로 표시
- 기존 scenario_b, action 태그 로직은 유지

### 검증
- 예약 실행 시 수동 실행과 동일한 결과
- reason이 `->` 형태로 정상 표시되는지
- 시나리오 A/B 포맷 정상 동작

---

## Phase 6: 통합 테스트 + 서버 배포

### 작업
1. 로컬에서 전체 흐름 점검 (코드 리뷰)
2. 커밋 + push
3. 서버 배포 (`git pull` + `systemctl restart`)
4. 실제 `/report` 명령 실행 확인

### 검증 항목
- Naver API 수집 정상 동작 (부서별 report_keywords, max_results=400)
- Haiku 필터로 부서 무관 기사/사진 캡션/중복 제거 확인
- Claude 분석 1회 호출로 구조화된 결과 반환
- 시나리오 A: 첫 요청 → 전체 브리핑 생성 + DB 저장
- 시나리오 B: 재요청 → 기존 항목 갱신/추가 + 누적 출력
- 후속 판단: 이틀치 이력 기반 follow_up/new 정상 분류
- 선택 사유: `->` 형태로 표시
- 예약 실행 정상
- 토큰 사용량 확인 (Langfuse) — 기존 대비 대폭 감소 확인

---

## 리스크 및 고려사항

### Naver API 키워드 커버리지
- web_search 대비 키워드 기반 검색은 커버리지가 좁을 수 있음
- 부서별 키워드를 충분히 넓게 정의하되, 추후 사용자 피드백으로 보완
- report_keywords는 config에서 관리하여 수정 용이

### Haiku 필터 정확도
- Haiku가 부서 관련성을 잘못 판단할 가능성 존재
- 대응: 필터 프롬프트에 부서 coverage를 명확히 제공 + "애매하면 포함" 지시
- 필요 시 임계값 조정 또는 프롬프트 개선으로 대응 가능

### 시나리오 B 누적 갱신 시 Claude 컨텍스트
- 시나리오 B에서는 기존 캐시 항목이 Claude 프롬프트에 포함됨
- 항목이 많아지면 프롬프트 크기가 증가하지만, 1회 호출이므로 기존 대비 여전히 소량
- 예상: 기존 캐시 10~15항목 × ~200자 = ~3K 추가 토큰 (무시할 수준)

### 3시간 윈도우와 누적 구조의 조화
- 매 요청마다 최근 3시간 기사만 수집하되, Claude에게 기존 캐시를 컨텍스트로 전달
- Claude가 "기존 항목에 추가할 새 팩트가 있는지" 판단 (시나리오 B)
- 이전 report에 없던 완전히 새로운 기사는 added로 추가

---

## API 테스트 결과 (2026-02-12)

실제 네이버 API 호출로 검증한 부서별 파이프라인 효율:

| 부서 | 수집 | 언론사필터 후 | 비고 |
|------|------|-------------|------|
| 사회부 | 200 | 66 (-67%) | 적정 범위 |
| 정치부 | 200 | 83 (-58%) | 다소 많음 → Haiku 필터로 정리 |
| 경제부 | 133 | 42 (-68%) | 적정 범위 |
| 산업부 | 200 | 34 (-83%) | 수집 확대(400)로 보강 필요 |
| 문화부 | 200 | 57 (-71%) | 크로스 오염 있음 → Haiku 필터로 해결 |
| 스포츠부 | 200 | 104 (-48%) | 사진 캡션 다수 → Haiku 필터로 해결 |

**발견 사항**:
- 언론사 필터가 가장 강력 (48~83% 제거)
- 크로스 오염 (문화부에 정치 기사 유입 등) 존재 → LLM 필터가 규칙 기반보다 정확하게 처리
- 스포츠부 사진 캡션 (6~16자 제목, description만 긴 포토뉴스) → LLM 필터로 자연 제거
- 5/6 부서가 수집 200건 상한 도달 → 400건으로 확대 필요
