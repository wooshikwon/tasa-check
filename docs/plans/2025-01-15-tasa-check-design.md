# 타사 체크 Agent 설계 문서

## 개요

기자를 위한 타사 뉴스 모니터링 도구. Telegram Bot으로 동작하며, 두 가지 명령을 제공한다:
- `/check`: 기자의 키워드 기반 네이버 뉴스 수집 → 언론사 필터 → Claude 분석 → [단독]/중요 기사 보고
- `/report`: 부서 뉴스 브리핑. Claude web search로 부서 관련 뉴스를 자율 검색하여 캐시 기반 브리핑 제공

## 핵심 결정사항

| 항목 | 결정 | 근거 |
|------|------|------|
| 플랫폼 | Telegram Bot | 모바일 접근 필수, 개발 용이성 최고 |
| LLM 비용 | BYOK (기자가 자기 API 키 등록) | 개발자 LLM 비용 $0 |
| 서버 | Oracle Cloud free tier 또는 로컬 | 서버 비용 $0, 디스크 영속성 보장 |
| 저장소 | SQLite | 서버리스, 단일 파일, PoC 적합 |
| /check 검색 | 네이버 검색 API (키워드 기반) | 무료, 일 25,000건, 한국 뉴스 특화 |
| /report 검색 | Anthropic API `web_search_20250305` 빌트인 | Claude가 자율적으로 검색 쿼리 생성 |
| /report 캐시 | 기자 개인별 | BYOK 비용 공정성 보장 |
| 메시지 형식 | 기사 1건당 Telegram 메시지 1개 | 4,096자 제한 대응 |
| /check → /report | 단방향 참조 | /check가 당일 report_items를 optional 맥락으로 활용 |

## 시스템 아키텍처

```
┌──────────────────────────────────────────────────────┐
│              Telegram Bot (단일 프로세스)               │
│                                                       │
│  /start  → ConversationHandler (프로필 단계별 등록)     │
│  /check  → 타사 체크 (키워드 기반 네이버 검색)          │
│  /report → 부서 뉴스 브리핑 (Claude web search)        │
│  /setkey → API 키 변경                                 │
└──────┬───────────────────────────────────┬────────────┘
       │                                   │
       ▼                                   ▼
┌──────────────┐                 ┌─────────────────┐
│  /check 흐름  │                 │  /report 흐름    │
│               │                 │                  │
│ ① 프로필 로드 │  report_items   │ ① 캐시 로드      │
│ ② 시간 윈도우 │  직접 로드      │ ② 에이전트 루프  │
│ ③ 네이버 API  │ <────────────── │   web_search +   │
│ ④ 언론사 필터 │  (optional)     │   fetch_article  │
│ ⑤ 본문 수집   │                 │ ③ 캐시 저장      │
│ ⑥ 맥락 로드   │                 │ ④ 기사별 전송    │
│ ⑦ 이력 로드   │                 │                  │
│ ⑧ 분석+전송   │                 │                  │
└──────┬───────┘                 └────────┬────────┘
       │                                   │
       ▼                                   ▼
┌──────────────────────────────────────────────────────┐
│                      SQLite                           │
│                                                       │
│  journalists       │ 프로필, 키워드, 부서, API 키      │
│  report_cache      │ 기자별 당일 /report 캐시 메타     │
│  report_items      │ 개별 뉴스 항목 캐시               │
│  reported_articles │ 기자별 /check 보고 이력 (중복제거) │
└──────────────────────────────────────────────────────┘
```

---

## /start: 프로필 등록

Telegram `ConversationHandler`로 단계별 입력을 유도한다.

```
기자: /start
Bot:  타사 체크 봇입니다. 이름을 알려주세요.

기자: 김철수
Bot:  담당 부서를 선택해주세요.
      [사회부] [정치부] [경제부] [문화부] [국제부]    <- Inline Keyboard

기자: (사회부 클릭)
Bot:  모니터링 키워드를 입력해주세요. (쉼표 구분)
      예: 서부지검, 서부지법, 영등포경찰서

기자: 서부지검, 서부지법, 영등포경찰서
Bot:  Anthropic API 키를 입력해주세요.
      (1:1 DM이므로 타인에게 노출되지 않습니다)

기자: sk-ant-xxx...
Bot:  설정 완료!
      이름: 김철수 | 부서: 사회부
      키워드: 서부지검, 서부지법, 영등포경찰서
      /check - 타사 체크 | /report - 부서 브리핑
```

### ConversationHandler 상태 머신

```
ENTRY -> NAME -> DEPARTMENT -> KEYWORDS -> API_KEY -> DONE
                  (Inline KB)   (텍스트)     (텍스트)
```

- 각 단계에서 입력 검증 (API 키 형식, 키워드 파싱 등)
- /setkey: API 키만 별도 변경 가능
- 프로필 재설정: /start 재실행으로 전체 덮어쓰기

---

## /check 적응형 시간 윈도우

/check의 마지막 호출 시각을 기자 개인별로 추적하여 중복 수집을 방지한다.

```
시간 윈도우 = min(현재 - 해당 기자의 마지막 /check 시각, 3시간)

예시:
  30분 전 /check -> 최근 30분 기사만 수집
  2시간 전 /check -> 최근 2시간 기사만 수집
  5시간 전 /check -> 최대 3시간 cap
  최초 /check     -> 3시간
```

이전 호출에서 이미 처리된 기사는 시간 윈도우 밖이므로 자연스럽게 제외된다.

---

## /check: 타사 체크

### 실행 흐름

```
/check 호출
    |
    v
[1] 프로필 로드
    DB에서 journalist_id로 조회
    -> keywords, department, api_key
    -> journalists.last_check_at 조회 -> 시간 윈도우 계산

[2] 네이버 뉴스 수집
    네이버 API 최대 2회 호출 (페이지네이션, 최대 200건 cap)
      query: "서부지검 | 서부지법 | 영등포경찰서" (키워드 OR 합산)
      1회: display=100, start=1,   sort=date
      2회: display=100, start=101, sort=date (1회로 충분하면 생략)
    반환된 결과 중 pubDate가 시간 윈도우 이내인 것만 후처리 필터
    (네이버 API에 시간 범위 파라미터 없음)
    시간 윈도우 밖 기사 도달 시 조기 종료

[3] 언론사 필터링 (규칙 기반)
    originallink 도메인을 publishers.json 화이트리스트와 대조
    비주요 언론사 기사 전부 제외
    LLM 호출 전이라 비용 $0

[4] 본문 수집
    남은 기사들의 n.news.naver.com URL에서 HTML 파싱
    첫~두 번째 문단까지만 추출하여 Claude context 절약
    병렬 처리로 속도 확보

[5] 맥락 로드 (optional)
    해당 기자의 당일 report_items 조회
    있으면: 사회적 맥락으로 Claude 분석에 전달
    없으면: 맥락 없이 분석 진행 (에러 아님, 정상 동작)

[6] 보고 이력 로드
    reported_articles에서 해당 기자의 최근 24시간 보고 이력

[7] Claude API 분석 (기자의 API 키 사용)
    Input:
      - 기사 목록 (제목 + 본문 첫~두 문단 + 언론사 + 시각)
      - 당일 report_items (있으면, 사회적 맥락으로 활용)
      - 기자의 이전 보고 이력 (reported_articles)
    처리:
      (1) 동일 배치 내 중복 병합
          같은 사안을 다룬 여러 언론사 기사 -> 가장 포괄적인 1건만 남김
      (2) 이전 보고 대비 중복 판단
          보고 이력에 존재하는 주제와 실질적으로 동일하면 스킵
          단, 중요한 새 팩트가 있으면 스킵하지 않고 보고
      (3) 중복 제거 후 남은 기사에 주요도 판단 (A~D 기준)
    Output:
      - 기사별: 요약 + 주요 판단 근거
      - 카테고리: [단독] / [주요] / [스킵]

[8] 결과 저장 + 기사별 전송
    보고된 기사의 요약, 핵심 팩트, 주제 클러스터를 reported_articles에 저장
    journalists.last_check_at 갱신 (결과와 무관하게 항상)
    기사 1건당 Telegram 메시지 1개로 전송 ([스킵]은 사용자에게 전송하지 않음)
```

### Claude API 프롬프트 구조

```
[시스템]
당신은 기자의 타사 체크 보조입니다.

[주요 기사 판단 기준]
아래 기준 중 하나 이상에 해당하면 [주요]로 판단한다:

A. 팩트 기반
  - 공식 조치: 체포, 구속, 기소, 영장 청구/기각, 판결, 정책 발표
  - 수치적 규모: 금액, 인원, 피해 규모가 유의미한 수준
  - 관계자 급: 고위 공직자, 대기업 임원, 공인 등
  - 중대한 전개: 소환->구속, 수사->기소 등 국면 전환

B. 경쟁 관점
  - 사실상 단독: [단독] 태그 없어도 특정 언론사만 보도한 기사
  - 복수 보도: 3개 이상 주요 언론사가 동시 보도
  - 새로운 앵글: 동일 사안에 대한 새로운 관점/정보

C. 사회적 맥락 (맥락이 제공된 경우)
  - 진행 중 주요 이슈와 직접 연결
  - 정책/법률 변경에 영향 가능
  - 후속 보도 가능성 높음

D. 시의성
  - 속보성: 방금 발생/확인된 사건
  - 임박 이벤트: 오늘/내일 중 결정/발표/공판 예정

[중복 제거 기준]
1. 동일 배치 내: 같은 사안의 여러 언론사 기사 -> 가장 포괄적인 1건만 남김
2. 이전 보고 대비: 아래 이력과 실질적으로 동일한 내용이면 스킵
3. 중복 예외: 이전 보고 주제라도 중요한 새 팩트(공식 조치, 수치 변경, 인물 추가 등)가 있으면 보고

[당일 사회적 맥락 - {부서}부]      <- report_items가 있을 때만 포함
(1) 서부지검 OO기업 수사 확대 중
(2) 검찰 수사권 조정 법안 본회의 예정
...

[기자의 최근 보고 이력]
- 14:30 보고: "서부지검 OO기업 수사"
  확인된 팩트: (1) 대표 소환 (2) 회계장부 압수
- 11:00 보고: "영등포서 XX 사건"
  확인된 팩트: (1) 피의자 체포 (2) 구속영장 청구

[새로 수집된 기사]
1. [한겨레] 서부지검, OO기업 수사 확대... 임원 3명 추가 소환
   본문(1~2문단): ...
2. [조선일보] 서부지검, OO기업 대표 소환 조사
   본문(1~2문단): ...
3. [연합뉴스] [단독] 서부지법, XX 사건 영장 기각
   본문(1~2문단): ...

각 기사에 대해:
1. 중복 제거: 동일 배치 내 병합 + 이전 보고 대비 중복 판단
2. [단독] 식별: 제목 태그 또는 사실상 단독 여부
3. 중복 아닌 기사에 주요도 판단 (A~D 기준 적용)
4. 보고 대상: 요약 + 해당되는 판단 근거 명시
```

### /check 출력 형식 (기사 1건 = 메시지 1개)

```
메시지 1:
  타사 체크 (YYYY-MM-DD HH:MM) - 주요 N건 (전체 M건 중)

메시지 2:
  [단독] [연합뉴스] 서부지법, XX 사건 영장 기각
  서부지법이 XX 사건 피의자에 대한 구속영장을 기각했다.
  판사는 "증거 인멸 우려가 소명되지 않았다"고 밝혔다.
  -> 중요 근거: 검찰 수사 동력에 직접적 영향
  https://...

메시지 3:
  [주요] [한겨레] 서부지검, OO기업 수사 확대
  기존 대표 소환에서 임원 3명 추가 소환으로 수사 확대.
  새로운 팩트: 임원 3명 추가 소환, 이번 주 내 영장 청구 검토
  -> 중요 근거: 기존 보고 대비 수사 확대라는 새로운 전개
  https://...
```

수집 결과가 없으면: "시간 윈도우 내 신규 기사가 없습니다."

---

## /report: 부서 뉴스 브리핑

news 스킬과 동일한 구조로 동작한다. Claude가 web search로 자율 검색하고, 캐시 기반으로 후속/신규를 분류한다.

### 에이전트 루프 구조

Anthropic API의 `web_search_20250305` 빌트인 도구와 커스텀 `fetch_article` 도구를 사용한다.
web_search는 Anthropic 서버에서 실행되고, fetch_article은 Python 백엔드에서 실행한다.

```python
tools = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 10
    },
    {
        "name": "fetch_article",
        "description": "URL의 기사 본문을 가져온다. 검색 결과 스니펫만으로 요약이 어려울 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"]
        }
    }
]

messages = [{"role": "user", "content": report_prompt}]

while True:
    response = claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        system=system_prompt,
        tools=tools,
        messages=messages
    )
    if response.stop_reason == "end_turn":
        break
    # web_search: Anthropic 서버에서 자동 실행
    # fetch_article: 백엔드에서 실행 후 결과 반환
    tool_results = execute_custom_tool_calls(response)
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})
```

### 캐시 단위: 기자 개인별

각 기자가 자기 API 키로 자기 캐시를 생성/갱신한다. 부서 공유 없음.

```
09:00  김기자(사회부) /report -> 김기자 캐시 생성 (김기자 키로 LLM + web search)
09:05  박기자(사회부) /report -> 박기자 캐시 생성 (박기자 키로 LLM + web search)
-> 각자 자기 비용 부담. 공정.
```

### 0단계: 캐시 로드

- report_cache + report_items에서 해당 기자의 데이터 조회
- 오늘 캐시 존재 -> 시나리오 B (당일 재요청)
  - 기존 항목의 제목, URL, 태그, 요약을 모두 로드
  - 기존 항목 요약은 3단계에서 그대로 재전달에 사용
- 오늘 캐시 없음 -> 시나리오 A (당일 첫 요청)
- 최근 3일 report_items에서 태그 추출 -> 후속 검색 입력으로 사용
  - 예: `#서부지검 #OO기업수사 #검찰수사권` -> 후속 쿼리 생성에 활용

### 1단계: 검색 (2트랙)

시스템 프롬프트에 부서 정보와 이전 태그를 제공하면, Claude가 자율적으로 적절한 쿼리를 생성하여 web_search를 실행한다.

**트랙A: 후속 검색** (캐시 기반)
- 0단계에서 추출한 태그를 조합하여 후속 검색 쿼리 생성
- 연속성이 강한 토픽의 후속 보도 검색
- 키워드 클러스터당 1~2개 쿼리, 최대 4~5개 실행

**트랙B: 부서별 신규 검색**
- 부서에 맞는 포괄적 뉴스 검색
- 검색 쿼리에 오늘 날짜(YYYY년 M월 D일) 명시
- Claude가 부서 정보를 기반으로 자율적으로 쿼리 구성

트랙A/B를 병렬로 실행한다.

### 2단계: 결과 정제

#### 시나리오 A (당일 첫 요청)

**후속/심화 분류 (트랙A 결과):**
- 이전 날짜 캐시 항목과 내용상 연결되는 후속 보도만 후속/심화로 분류
- 연결된 이전 항목의 제목을 `(이전 전달: YYYY-MM-DD "제목")` 형태로 명시
- 내용상 연결이 없으면 신규로 분류
- 후속/심화 항목이 0개이면 해당 섹션 생략

**신규 분류 (트랙A 잔여 + 트랙B 결과):**
- 기사 발행일이 당일인 것만 선별 (전일 뉴스 포함 안 함)
- 동일 사안의 다른 출처 기사는 하나로 병합

#### 시나리오 B (당일 재요청)

**기존 항목 갱신 판정:**
- 검색 결과 중, 오늘 캐시의 기존 항목과 동일 토픽의 후속 정보가 있으면:
  - 기존 요약을 후속 정보를 반영하여 갱신
  - `[수정]` 표시

**신규 항목 판정:**
- 기존 캐시 URL과 중복되지 않는 새로운 항목은 `[추가]` 표시
- 기사 발행일이 당일인 것만 선별

**변경 없는 기존 항목:**
- 캐시된 요약을 그대로 사용 (표시 없음)

**전체 목표: 최소 5개**
- 부족하면 Claude가 추가 검색 자율 판단

### 3단계: 캐시 저장

#### 시나리오 A
- report_cache에 새 레코드 생성 (journalist_id + 오늘 날짜)
- 모든 항목을 report_items에 저장 (제목, URL, 요약, 태그, 카테고리)

#### 시나리오 B
- `[추가]` 항목: report_items에 새 레코드 추가
- `[수정]` 항목: 기존 report_items 레코드의 summary, updated_at 갱신
- 변경 없는 항목: 그대로 유지
- `[추가]`와 `[수정]`이 모두 없으면 캐시 갱신 생략

### 4단계: 출력 (기사 1건 = 메시지 1개)

#### 시나리오 A (당일 첫 요청)

```
메시지 1:
  사회부 주요 뉴스 (YYYY-MM-DD) - 총 N건

메시지 2:
  [후속] 서부지검 OO기업 수사 확대
  서부지검 형사부가 OO기업 임원 3명을 추가 소환했다.
  기존 대표 소환 조사에서 회계 부정 정황이 추가 포착된 것으로 알려졌다.
  검찰은 이번 주 내 구속영장 청구 여부를 결정할 방침이다.
  (이전 전달: YYYY-MM-DD "서부지검 OO기업 대표 소환")
  https://...

메시지 3:
  [신규] 영등포서, XX 사건 피의자 긴급체포
  영등포경찰서가 XX 사건 핵심 피의자를 긴급체포했다.
  ...
  https://...
```

#### 시나리오 B (당일 재요청)

변경 없는 기존 항목은 전송하지 않음. [수정]/[추가] 항목만 전송.

```
메시지 1:
  사회부 뉴스 업데이트 (HH:MM) - 수정 N건, 추가 M건

메시지 2:
  [수정] 영등포서 XX 사건
  (기존 요약 + 새 정보 병합)
  https://...

메시지 3:
  [추가] 서부지법, YY 사건 영장 기각
  (새 요약)
  https://...
```

변경 없으면: "업데이트 없음. 이전 브리핑과 동일합니다."

### /report 시스템 프롬프트 구조

```
[시스템]
당신은 {부서}부 기자의 뉴스 브리핑 보조입니다.
web_search로 뉴스를 검색하고, 검색 결과 스니펫만으로 요약이
어려우면 fetch_article로 원문을 읽어 보충합니다.

[검색 범위]
- 당일(YYYY-MM-DD) 한국 뉴스만 대상
- 전일 이전 기사는 포함하지 않음

[이전 전달 태그 - 최근 3일]
#서부지검 #OO기업수사 #검찰수사권 ...

[오늘 기존 캐시]                    <- 시나리오 B일 때만 포함
1. 서부지검 OO기업 수사 확대 | 요약: ... | #서부지검 #OO기업수사
2. ...

{부서}부 관련 당일 주요 뉴스를 검색하여 브리핑을 작성하시오.

절차:
1. 이전 태그 기반 후속 검색 + 부서별 신규 검색을 실행
2. 검색 쿼리에 오늘 날짜(YYYY년 M월 D일)를 명시
3. 후속/심화: 이전 캐시 항목과 내용상 연결되는 보도
4. 신규: 연결 없는 새로운 뉴스
5. 최소 5개 항목 목표. 부족하면 추가 검색.
6. 각 항목: 제목 + 3~5줄 요약 + URL + 태그(2~4개)
```

---

## 요약 작성 기준 (공통)

/check와 /report 모두 동일한 기준으로 요약을 작성한다.

- 구체적 정보 전달: "수사가 확대됐다" 대신 "임원 3명을 추가 소환했다"
- 핵심 수치/사실 포함: 인물명, 기관명, 일시 등
- 맥락 제공: 이 뉴스가 왜 중요한지 한 문장으로 짚는다
- [수정] 항목: 기존 요약을 삭제하지 않고 새 정보를 병합
- 사실 기반 작성, 추측/의견 배제
- 검색 결과 스니펫만으로 요약이 어려우면 원문을 읽어 보충

---

## 데이터 모델

### journalists

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| telegram_id | TEXT UNIQUE | Telegram 사용자 ID |
| name | TEXT | 기자 이름 |
| department | TEXT | 부서 (사회부, 정치부 등) |
| keywords | TEXT (JSON) | 검색 키워드 목록 (/check용) |
| api_key | TEXT | Anthropic API 키 (암호화) |
| last_check_at | DATETIME | 마지막 /check 시각 (시간 윈도우 기준, 결과 무관 갱신) |
| created_at | DATETIME | |

### report_cache (기자 개인별, /report 메타)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| journalist_id | INTEGER FK | journalists 참조 |
| date | DATE | 날짜 |
| updated_at | DATETIME | 마지막 /report 시각 |

UNIQUE 제약: (journalist_id, date)

### report_items (개별 뉴스 항목, /report 캐시)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| report_cache_id | INTEGER FK | report_cache 참조 |
| title | TEXT | 뉴스 제목 |
| url | TEXT | 원본 기사 URL |
| summary | TEXT | 3~5줄 요약 (캐시, 재요청 시 그대로 재전달) |
| tags | TEXT (JSON) | 키워드 태그 (후속 검색용) |
| category | TEXT | "follow_up" / "new" |
| prev_reference | TEXT | 후속 항목일 경우 이전 항목 제목+날짜 |
| created_at | DATETIME | 최초 수집 시각 |
| updated_at | DATETIME | 마지막 갱신 시각 |

### reported_articles (기자별, /check 중복 제거용)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | INTEGER PK | |
| journalist_id | INTEGER FK | journalists 참조 |
| checked_at | DATETIME | /check 실행 시각 (시간 윈도우 기준) |
| topic_cluster | TEXT | 주제 식별자 |
| key_facts | TEXT (JSON) | LLM이 추출한 핵심 팩트 목록 |
| summary | TEXT | LLM이 생성한 요약 |
| article_urls | TEXT (JSON) | 원본 기사 URL 목록 |
| category | TEXT | "exclusive" / "important" |

### 캐시 관리

- 14일 이상 지난 report_items 자동 삭제
- 14일 이상 지난 reported_articles 자동 삭제
- 앱 시작 시 또는 일 1회 정리

---

## 기술 스택

| 구성 요소 | 기술 | 비고 |
|-----------|------|------|
| 언어 | Python 3.12+ | |
| 패키지 관리 | uv | |
| Telegram | python-telegram-bot | ConversationHandler 활용 |
| LLM | anthropic SDK | /check: 단일 호출, /report: web_search 에이전트 루프 |
| HTTP | httpx | 네이버 API + 기사 본문 수집 |
| HTML 파싱 | BeautifulSoup4 | n.news.naver.com 본문 추출 |
| 저장소 | SQLite (aiosqlite) | 비동기 접근 |

## 프로젝트 구조

```
tasa-check/
├── pyproject.toml
├── main.py                        # 진입점 (Telegram bot 실행)
├── src/
│   ├── bot/
│   │   ├── handlers.py            # /check, /report, /setkey 핸들러
│   │   ├── conversation.py        # ConversationHandler (프로필 등록)
│   │   └── formatters.py          # Telegram 메시지 포맷팅 (기사별 메시지)
│   ├── agents/
│   │   ├── report_agent.py        # /report 에이전트 루프 (web_search 기반)
│   │   └── check_agent.py         # /check 기사 분석 (단일 Claude API 호출)
│   ├── tools/
│   │   ├── search.py              # search_news (네이버 API, /check 전용)
│   │   └── scraper.py             # fetch_article (기사 본문 추출)
│   ├── filters/
│   │   └── publisher.py           # 언론사 화이트리스트 필터 (/check 전용)
│   ├── storage/
│   │   ├── models.py              # DB 스키마 + 초기화
│   │   └── repository.py          # 데이터 접근 계층
│   └── config.py                  # 환경변수, 설정
└── data/
    └── publishers.json            # 주요 언론사 목록 (23개)
```

## 비용 분석

기자 10명, 하루 각 5회 /check + 3회 /report 기준:

| 항목 | 비용 | 부담 |
|------|------|------|
| 네이버 API | $0 | - |
| Telegram Bot API | $0 | - |
| 서버 (Oracle Cloud free tier) | $0 | 개발자 |
| /check (기자당 5회/일) | ~$0.02 x 5 = $0.10 | 각 기자 (BYOK) |
| /report (기자당 3회/일, web search 포함) | ~$0.05 x 3 = $0.15 | 각 기자 (BYOK) |
| **기자 1인당 일일 LLM 비용** | **~$0.25** | **기자 (BYOK)** |
| **개발자 서버 비용** | **$0** | |

/report 비용은 web search 호출 횟수에 따라 변동. 실제 PoC에서 측정 후 조정.

## 언론사 화이트리스트

`data/publishers.json`에 23개 주요 언론사 정의 (/check 전용):

| 분류 | 언론사 | 수 |
|------|--------|---|
| 종합일간지 | 조선, 중앙, 동아, 한겨레, 경향, 한국일보, 국민일보 | 7 |
| 석간 | 문화일보, 서울신문, 세계일보 | 3 |
| 경제지 | 매일경제, 한국경제 | 2 |
| 지상파 | KBS, MBC, SBS | 3 |
| 종편 | JTBC, 채널A, TV조선, MBN | 4 |
| 보도전문 | YTN | 1 |
| 통신사 | 연합뉴스, 뉴시스, 뉴스1 | 3 |

## 배포/운영

### 사전 준비 (외부 서비스)

| 항목 | 발급 방법 | 비용 |
|------|-----------|------|
| Telegram Bot Token | BotFather(@BotFather)에서 `/newbot` 명령으로 생성 | $0 |
| 네이버 검색 API | [developers.naver.com](https://developers.naver.com) 앱 등록 → Client ID + Secret | $0 (일 25,000건) |
| Anthropic API 키 | 기자 각자 발급 (BYOK) | 개발자 불필요 |

### 서버

Oracle Cloud Always Free tier ARM 인스턴스를 사용한다.

| 항목 | 선택 | 근거 |
|------|------|------|
| 인스턴스 | VM.Standard.A1.Flex (ARM) | Always Free, 충분한 성능 |
| 스펙 | 1 OCPU / 6GB RAM | PoC 10명 기준 충분 |
| OS | Ubuntu 22.04 | 커뮤니티 지원 최고, ARM 호환 |
| 디스크 | 50GB (기본 부트 볼륨) | SQLite + 로그 충분 |

### 실행 환경

```
# Python: uv가 자체 관리 (시스템 Python 불필요)
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /opt/tasa-check
uv sync
```

### Telegram 연결 방식

**Polling** 방식을 사용한다. 도메인/SSL 불필요, PoC에 적합.

```python
# main.py
application = Application.builder().token(BOT_TOKEN).build()
application.run_polling()
```

webhook은 도메인 구매 + SSL 인증서 설정이 필요하므로 PoC에서는 불필요한 복잡도.

### 프로세스 관리

systemd 서비스로 등록한다. 자동 시작, 크래시 복구 포함.

```ini
# /etc/systemd/system/tasa-check.service
[Unit]
Description=Tasa Check Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/tasa-check
ExecStart=/opt/tasa-check/.venv/bin/python main.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/tasa-check/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable tasa-check
sudo systemctl start tasa-check
# 로그 확인: journalctl -u tasa-check -f
```

### 환경변수

`.env` 파일로 관리한다. git에 포함하지 않음.

```
TELEGRAM_BOT_TOKEN=...
NAVER_CLIENT_ID=...
NAVER_CLIENT_SECRET=...
FERNET_KEY=...
DB_PATH=/opt/tasa-check/data/tasa-check.db
```

### API 키 암호화

기자의 Anthropic API 키는 Fernet 대칭 암호화로 저장한다.

| 항목 | 선택 |
|------|------|
| 방식 | Fernet (cryptography 패키지) |
| 암호화 키 | `.env`의 `FERNET_KEY` |
| 키 생성 | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

DB에는 암호화된 값만 저장. Claude API 호출 시 복호화하여 사용.

### 데이터 파일 위치

```
/opt/tasa-check/
├── data/
│   ├── tasa-check.db          # SQLite (자동 생성)
│   └── publishers.json        # 언론사 화이트리스트
├── .env                       # 환경변수 (git 제외)
└── ...
```

### 배포 절차

```bash
# 1. 서버 접속
ssh ubuntu@<oracle-cloud-ip>

# 2. 코드 배포
cd /opt/tasa-check
git pull origin main

# 3. 의존성 동기화
uv sync

# 4. 서비스 재시작
sudo systemctl restart tasa-check
```

---

## PoC 단계 구분

### Phase 1 (핵심)
- /start: ConversationHandler로 프로필 등록 (이름, 부서, 키워드, API 키)
- /check: 네이버 수집 -> 언론사 필터 -> 본문 추출 -> Claude 분석 -> 기사별 전송
- 적응형 시간 윈도우 (기자 개인별)
- 의미 기반 중복 제거 (기자별 reported_articles)
- /setkey: API 키 변경

### Phase 2 (브리핑)
- /report: Claude web search 기반 부서 뉴스 브리핑 (기자 개인별 캐시)
- 캐시 정책: 당일 첫 호출 (시나리오 A) / 재호출 (시나리오 B)
- /check가 report_items를 사회적 맥락으로 활용 (Phase 1에서는 맥락 없이 동작)
- 14일 초과 캐시 자동 정리
