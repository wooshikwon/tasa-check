# Phase 2 개발 방향서

## 목적

Phase 1은 `/check` (키워드 기반 네이버 검색 + Claude 분석)을 구현했다.
Phase 2는 `/report` (Claude web search 기반 부서 뉴스 브리핑)를 구현하고,
`/check`가 `/report` 결과를 사회적 맥락으로 활용하는 연결을 완성한다.

## Phase 2 범위

- /report: Claude web search 기반 부서 뉴스 브리핑 (기자 개인별 캐시)
- 캐시 정책: 당일 첫 요청 (시나리오 A) / 당일 재요청 (시나리오 B)
- /check ↔ /report 맥락 연결 (Phase 1에서는 report_items가 비어있어 미동작)
- 14일 초과 캐시 자동 정리 (Phase 1에서 이미 구현, 동작 검증만 필요)

## Phase 1에서 이미 구현된 기반

Phase 2는 기존 코드 위에 증분으로 구현한다. 중복 생성 없이 기존 모듈을 확장한다.

| 구성 요소 | 현황 | Phase 2 작업 |
|-----------|------|-------------|
| DB 스키마 (models.py) | report_cache, report_items 테이블 존재 | 변경 없음 |
| repository.py | get_today_report_items, cleanup_old_data 존재 | report_cache/report_items CRUD 추가 |
| scraper.py | fetch_article_body 함수 존재 | /report의 fetch_article 도구로 재사용 |
| config.py | CACHE_RETENTION_DAYS, DEPARTMENTS 존재 | 변경 없음 |
| formatters.py | /check 포맷 함수 존재 | /report 포맷 함수 추가 |
| handlers.py | check_handler, setkey_handler 존재 | report_handler 추가 |
| main.py | /check, /setkey 등록 | /report 등록 추가 |

---

## 모듈 의존성

```
Phase 1 기존 모듈 (수정 대상)
  storage/repository.py ─── report_cache/report_items CRUD 추가
  bot/formatters.py     ─── /report 포맷 함수 추가
  bot/handlers.py       ─── report_handler 추가
  main.py               ─── /report 명령 등록

Phase 2 신규 모듈
  agents/report_agent.py ←── repository + scraper + config
```

의존 관계:
```
repository CRUD (Step 1)
       ↓
report_agent.py (Step 2) ←── scraper.fetch_article_body (기존)
       ↓
formatters.py /report 포맷 (Step 3, report_agent와 독립)
       ↓
handlers.py + main.py (Step 4) ←── report_agent + formatters + repository
```

---

## 개발 순서

### Step 1: Repository 확장 — report_cache/report_items CRUD

| 항목 | 내용 |
|------|------|
| 작업 | repository.py에 /report용 CRUD 함수 추가 |
| agent 전략 | 순차 개발 (기존 코드 확장) |
| 테스트 | 각 함수별 단위 테스트 |

추가할 함수:

```
get_or_create_report_cache(db, journalist_id, date)
  -> (cache_id, is_new)
  # 시나리오 A/B 분기 판단 기준

get_report_items_by_cache(db, report_cache_id)
  -> list[dict]
  # 시나리오 B에서 기존 캐시 항목 로드

save_report_items(db, report_cache_id, items)
  # 시나리오 A: 전체 항목 저장
  # 시나리오 B: [추가] 항목 저장

update_report_item(db, item_id, summary)
  # 시나리오 B: [수정] 항목 요약 갱신

get_recent_report_tags(db, journalist_id, days=3)
  -> list[str]
  # 최근 3일 report_items 태그 추출 (후속 검색용)
```

리뷰 체크포인트:
- 기존 get_today_report_items, cleanup_old_data와 중복 없는지 확인
- report_cache UNIQUE(journalist_id, date) 제약 활용한 upsert 패턴

### Step 2: report_agent.py — 에이전트 루프 구현

| 항목 | 내용 |
|------|------|
| 작업 | agents/report_agent.py 신규 생성 |
| agent 전략 | 순차 개발 (핵심 모듈, 분할 불가) |
| 테스트 | Claude API mock 에이전트 루프, 도구 호출 처리 |

핵심 구조 — Anthropic API 에이전트 루프:

```python
tools = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 10
    },
    {
        "name": "fetch_article",
        "description": "URL의 기사 본문을 가져온다.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    }
]

# 에이전트 루프
while True:
    response = await client.messages.create(...)
    if response.stop_reason == "end_turn":
        # 최종 텍스트 응답 파싱
        break
    # tool_use 블록 처리:
    #   web_search → Anthropic 서버에서 자동 실행 (결과가 response에 포함)
    #   fetch_article → scraper.fetch_article_body() 호출 후 결과 반환
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})
```

구현 세부:
- 시스템 프롬프트: 설계 문서의 /report 시스템 프롬프트 구조 그대로 사용
- 시나리오 A (캐시 없음): 후속 태그 + 부서 신규 검색
- 시나리오 B (캐시 있음): 기존 캐시 정보를 프롬프트에 포함, [수정]/[추가] 판정
- Claude 응답 파싱: JSON 구조화된 출력 (각 항목: title, url, summary, tags, category, prev_reference)
- fetch_article: 기존 scraper.fetch_article_body를 재사용

리뷰 체크포인트:
- 에이전트 루프가 무한 루프에 빠지지 않도록 최대 턴 수 제한
- web_search 도구의 server_tool_use / web_search_tool_result 블록 처리
- fetch_article 결과를 tool_result로 올바르게 반환하는지 검증

### Step 3: formatters.py 확장 — /report 출력 포맷

| 항목 | 내용 |
|------|------|
| 작업 | formatters.py에 /report용 포맷 함수 추가 |
| agent 전략 | Step 2와 독립, 병렬 가능 |
| 테스트 | 시나리오 A/B 포맷 단위 테스트 |

추가할 함수:

```
format_report_header_a(department, date, count)
  # 시나리오 A: "사회부 주요 뉴스 (YYYY-MM-DD) - 총 N건"

format_report_header_b(time, modified, added)
  # 시나리오 B: "사회부 뉴스 업데이트 (HH:MM) - 수정 N건, 추가 M건"

format_report_item(item)
  # 기사 1건 HTML 포맷:
  # [후속]/[신규]/[수정]/[추가] 태그 + 제목 + 요약 + 이전 참조 + URL

format_report_no_update()
  # 시나리오 B 변경 없음: "업데이트 없음. 이전 브리핑과 동일합니다."
```

### Step 4: handler + main.py — /report 명령 연결

| 항목 | 내용 |
|------|------|
| 작업 | handlers.py에 report_handler 추가, main.py에 명령 등록 |
| agent 전략 | 순차 개발 (Step 2, 3 완료 후) |
| 테스트 | 통합 테스트 + 실제 Telegram 수동 검증 |

report_handler 흐름:

```
/report 호출
  ├─ [1] 프로필 로드 (기존 get_journalist)
  ├─ [2] 캐시 확인 (get_or_create_report_cache)
  │     ├─ is_new=True  → 시나리오 A
  │     └─ is_new=False → 시나리오 B (기존 항목 로드)
  ├─ [3] 최근 태그 로드 (get_recent_report_tags)
  ├─ [4] report_agent 실행 (에이전트 루프)
  ├─ [5] 캐시 저장/갱신
  │     ├─ 시나리오 A: save_report_items (전체)
  │     └─ 시나리오 B: save_report_items ([추가]) + update_report_item ([수정])
  └─ [6] 기사별 전송
        ├─ 시나리오 A: 전체 항목 전송
        └─ 시나리오 B: [수정]+[추가] 항목만 전송
```

main.py 변경:
```python
from src.bot.handlers import check_handler, setkey_handler, report_handler
app.add_handler(CommandHandler("report", report_handler))
```

### Step 5: 통합 검증 + /check 맥락 연결 확인

| 항목 | 내용 |
|------|------|
| 작업 | 전체 플로우 수동 테스트 |
| agent 전략 | code-review agent로 최종 검증 |

/check ↔ /report 맥락 연결 검증:
- /report 실행 → report_items에 데이터 저장됨
- /check 실행 → handlers.py의 [5] 맥락 로드에서 report_items 조회
- check_agent 프롬프트에 "[당일 사회적 맥락]" 섹션이 포함되는지 확인
- Phase 1에서 이미 코드가 존재하므로, 데이터만 있으면 자동으로 동작

---

## Agent 활용 전략

| 단계 | 방식 | 근거 |
|------|------|------|
| Step 1 | 순차 개발 | 기존 repository.py 확장, 일관성 중요 |
| Step 2 | 순차 개발 | 에이전트 루프가 핵심, 분할 불가 |
| Step 3 | **Step 2와 병렬 가능** | formatters는 입출력 스키마만 합의되면 독립 |
| Step 4 | 순차 개발 | Step 2+3 완료 후 연결 |
| Step 5 | code-review agent | 설계 문서 대비 전체 검증 |

병렬 실행 가능 구간:
```
Step 2 (report_agent.py) ──┐
                            ├─→ Step 4 (handler + main.py)
Step 3 (formatters.py)  ───┘
```

---

## 평가 체계

### 1. 자동 테스트 (pytest)

| 모듈 | 테스트 항목 |
|------|------------|
| repository (확장) | report_cache 생성/조회, report_items CRUD, 태그 추출 |
| report_agent | 에이전트 루프 mock (tool_use 응답 처리, fetch_article 호출, 최종 파싱) |
| formatters (확장) | 시나리오 A/B 헤더, 항목 포맷, HTML 이스케이프 |

### 2. 수동 검증 체크리스트

```
/report 시나리오 A (당일 첫 요청):
  [ ] /report 실행 → "브리핑 생성 중..." 메시지
  [ ] 헤더 메시지: "사회부 주요 뉴스 (날짜) - 총 N건"
  [ ] 기사별 개별 메시지 ([후속]/[신규] 태그 포함)
  [ ] 각 기사에 3~5줄 요약 + URL + 태그 포함
  [ ] 최소 5개 항목
  [ ] DB에 report_cache + report_items 저장 확인

/report 시나리오 B (당일 재요청):
  [ ] 즉시 재실행 → 헤더: "사회부 뉴스 업데이트 (시각)"
  [ ] [수정] 항목: 기존 요약에 새 정보 병합
  [ ] [추가] 항목: 새로운 기사
  [ ] 변경 없는 기존 항목은 전송 안 됨
  [ ] 변경 없으면 "업데이트 없음" 메시지

/check ↔ /report 맥락 연결:
  [ ] /report 실행 후 /check 실행
  [ ] /check 결과에 사회적 맥락이 반영되는지 확인
  [ ] 맥락이 분석 품질에 영향을 주는지 비교
```

### 3. 코드 리뷰 체크포인트

| 시점 | 검증 항목 |
|------|----------|
| Step 1 완료 | repository CRUD가 DB 스키마와 정합, 기존 함수와 중복 없음 |
| Step 2 완료 | 에이전트 루프 = 설계 문서 구조, 도구 처리 정확성, 무한 루프 방지 |
| Step 3 완료 | 출력 포맷 = 설계 문서 형식, HTML 이스케이프, 4096자 제한 |
| Step 4 완료 | /report 전체 흐름 = 설계 문서, 시나리오 A/B 분기 정확성 |
| Step 5 완료 | /check 맥락 연결, 전체 수동 체크리스트 통과 |

---

## Phase 2 완료 기준

아래 조건을 **모두** 충족해야 Phase 2 완료:

1. `pytest` 전체 통과 (Phase 1 기존 + Phase 2 신규)
2. 수동 검증 체크리스트 전항 통과
3. /report 시나리오 A, B 모두 정상 동작
4. /check가 /report 맥락을 활용하여 분석 (연결 검증)
5. 코드 리뷰 5회 완료 (Step별 1회)
6. 설계 문서와 구현 간 차이점 문서화 (있을 경우)
