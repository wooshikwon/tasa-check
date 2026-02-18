# LLM Agent 상세 설계

이 문서는 tasa-check 프로젝트에서 기사 분석을 수행하는 두 LLM Agent의 내부 구현을 코드 레벨로 해부한다.

---

## 1. check_agent.py -- 타사 체크 분석 에이전트

파일: `src/agents/check_agent.py`

`/check` 명령에서 호출되며, 수집된 기사 목록을 Claude API로 분석하여 [단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다. Haiku 사전 필터로 키워드 무관 기사를 제거한 뒤, Haiku로 분석한다.

### 1.1 analyze_articles() 함수

```python
async def analyze_articles(
    api_key: str,
    articles: list[dict],
    history: list[dict],
    department: str,
    keywords: list[str] | None = None,
) -> list[dict]:
```

**파라미터:**

| 파라미터 | 타입 | 설명 |
|---|---|---|
| `api_key` | `str` | 기자의 Anthropic API 키 |
| `articles` | `list[dict]` | 수집된 기사 목록 (title, publisher, body, url, pubDate) |
| `history` | `list[dict]` | 최근 보고 이력 (보고 + skip 이력 모두 포함) |
| `department` | `str` | 기자 부서명 |
| `keywords` | `list[str] \| None` | 기자의 취재 키워드 목록 |

**모델 설정:**

| 항목 | 값 |
|---|---|
| 모델 | `claude-haiku-4-5-20251001` |
| Temperature | `0.0`~`0.4` (재시도마다 0.1씩 증가) |
| Max tokens | `16384` |
| tool_choice | `{"type": "tool", "name": "submit_analysis"}` (강제 도구 호출) |
| 재시도 | 최대 5회 (파싱 실패 시 temperature 점진적 증가) |

**반환값:** 주요 항목(results)과 스킵 항목(skipped)을 병합한 단일 리스트. 스킵 항목에는 `category: "skip"`이 자동 부여된다.

**응답 파싱 로직:** `_parse_analysis_response()` 함수에서 `message.content`의 `tool_use` 블록을 순회하며 `submit_analysis` 이름의 블록을 찾는다. `_try_parse_json_field()`로 LLM이 배열을 JSON 문자열로 반환하는 경우도 처리한다. `results`와 `skipped` 각각에서 `dict` 타입만 필터링하고, 타입 불일치 발생 시 경고 로그를 남긴다. `skipped` 항목에 `category: "skip"`을 주입한 뒤 `results + skipped`로 병합 반환한다. 파싱 실패 시 `None`을 반환하여 재시도를 트리거한다.

### 1.1b filter_check_articles() 함수 -- Haiku 사전 필터

```python
async def filter_check_articles(
    api_key: str,
    articles: list[dict],
    department: str,
) -> list[dict]:
```

/check 파이프라인에서 본문 스크래핑 전에 호출되는 Haiku LLM 사전 필터. 제목+description만으로 부서 관련성을 판단하여 무관한 기사를 제거한다.

**모델 설정:**

| 항목 | 값 |
|---|---|
| 모델 | `claude-haiku-4-5-20251001` |
| Temperature | `0.0` |
| Max tokens | `2048` |
| tool_choice | `{"type": "tool", "name": "filter_news"}` (강제 도구 호출) |

**필터 기준 (시스템 프롬프트):**
1. 부서 관련성: 해당 부서 취재 영역에 해당하는 기사만 포함
2. 사진 캡션 제외
3. 명백한 홍보성 제외: 보도자료 전재, 기업 자체 수상/CSR 홍보, 할인/이벤트 안내 등 (대규모 투자/M&A/정책 변화는 제외하지 않음)
4. 애매한 경우 포함 쪽으로 판단

시스템 프롬프트에 `DEPARTMENT_PROFILES`의 `coverage`와 `criteria`를 모두 주입한다.

**도구 스키마:** `/report`의 `filter_news`와 동일한 `_CHECK_FILTER_TOOL` 사용. `selected_indices` (int 배열) 반환.

**Langfuse 스팬:** `"check_filter"`, metadata: `{"department": department, "input_count": len(articles)}`

### 1.2 시스템 프롬프트 구성 (_build_system_prompt)

```python
def _build_system_prompt(keywords: list[str], department: str) -> str:
```

`keywords`와 `department`를 받아 `_SYSTEM_PROMPT_TEMPLATE`에 다음 4개 변수를 주입한다:

| 템플릿 변수 | 출처 |
|---|---|
| `{dept_label}` | `_dept_label(department)` -- 부서명에 "부"가 없으면 자동 부착 |
| `{keywords_section}` | `", ".join(keywords)` (없으면 "(키워드 없음)") |
| `{coverage_section}` | `DEPARTMENT_PROFILES[dept_label]["coverage"]` |
| `{criteria_section}` | `DEPARTMENT_PROFILES[dept_label]["criteria"]`를 `"- "` 접두사로 줄바꿈 연결 |

시스템 프롬프트는 다음 섹션들로 구성된다:

**역할 정의:**
```
당신은 {dept_label} 기자의 타사 체크 보조입니다.
```

**키워드 관련성 필터 (최우선 기준):**
키워드 검색 API 특성상 무관한 기사가 포함될 수 있으므로, 기사 내용이 키워드와 "직접 관련"된 경우에만 판단 대상으로 삼도록 지시한다. "직접 관련"의 정의:
- 해당 키워드의 기관/장소/인물이 기사에 실제 등장
- 해당 관할/소관 사안을 다루는 경우
- 동일 분야라도 다른 기관/관할은 무관으로 처리
- 상위/하위/동급 다른 기관은 별개로 취급 (예: "서울경찰청" 키워드에 충북경찰청은 skip)
- 키워드 무관 기사는 기사 가치와 무관하게 반드시 skip

**주요 기사 판단 기준:**
키워드 관련성을 통과한 기사에 한해 적용. 부서별 `criteria`가 주입되며, 추가로 다음 공통 기준이 포함된다:
- 경쟁 관점: 사실상 단독, 복수 보도(3개 이상), 새로운 앵글
- 사회적 맥락: 진행 중 주요 이슈와 직접 연결, 후속 보도 가능성
- 시의성: 방금 발생/확인된 사건, 오늘/내일 중 결정 예정

**단독 기사 식별 (최우선 선정 대상):**
- 제목에 `[단독]` 태그 --> 무조건 선정
- 본문에 "OO 취재에 따르면", "본지 취재 결과" 등 `취재에 따르면` 패턴 --> 사실상 단독
- 본문 어미 기반 가치 판단:
  - "알려졌다", "전해졌다" --> 풍문 수준
  - "나타났다", "드러났다" --> 객관적 사실/공식 발표
  - "취재에 따르면", "확인됐다" --> 자체 취재/신규 팩트, 가장 높은 뉴스 가치

**중복 제거 기준:**
1. 동일 배치 내: 같은 사안 여러 언론사 기사 --> 가장 포괄적인 1건만 남김
2. 이전 보고 대비: 이력과 동일한 주제면 skip. 새로운 앵글/관점/추가 디테일(수치, 반응, 후속 보도 등)은 새 사안이 아니다
3. 유일한 중복 예외: [단독] 태그 또는 "취재에 따르면" 패턴이 있는 단독 기사만 재보고. 그 외 어떤 사유로도 이전 보고 주제를 재보고하지 않는다

**이전 skip 기사 승격 제한:**
이전에 skip 판정된 주제와 동일한 기사는 원칙적으로 skip 유지. skip 사유를 뒤집을 새로운 정보(공식 발표, 수사 진전, 복수 언론 보도 전환 등)가 있을 때만 승격한다.

**제외 기준:**
키워드와 관련되더라도 아래에 해당하면 skip:
- 홍보성 사용 통계: 기업이 배포한 이용자 수 등 마케팅성 수치
- 생활/문화 트렌드: 기술 제품의 일상 활용 사례, 시즌별 이용 패턴 소개
- 보도자료 단순 전재: 비즈니스 임팩트 없이 기업 발표 수치만 나열한 기사
- 단발성 사건/사고, 정례적 발표, 인터뷰/칼럼/사설, 연예/스포츠 가십
- 자기 검증: reason에 '홍보성', '트렌드' 등을 적게 된다면 해당 기사는 skip 대상

**요약 작성 기준:**
- 구체적 정보 우선 ("수사가 확대됐다" 대신 "임원 3명을 추가 소환했다")
- 핵심 수치/사실 포함 (인물명, 기관명, 일시 등)
- 맥락 제공 (왜 중요한지 한 문장)
- 사실 기반, 추측/의견 배제

### 1.3 사용자 프롬프트 구성 (_build_user_prompt)

```python
def _build_user_prompt(
    articles: list[dict],
    history: list[dict],
    department: str,
) -> str:
```

사용자 프롬프트는 다음 4개 섹션을 `"\n\n"` 구분자로 연결하여 구성된다.

**[기자의 최근 보고 이력]:**
`history`에서 `category != "skip"`인 항목만 추출. 각 항목을 다음 형식으로 포맷:
```
- {KST시각} 보고: "{topic_cluster}"
  확인된 팩트: (1) {fact1}, (2) {fact2}
```
이력이 없으면 "이력 없음"으로 표시. 시각은 `_to_kst()` 함수로 UTC ISO 문자열을 `YYYY-MM-DD HH:MM` KST 형식으로 변환한다.

**[이전 skip 이력 - 동일 주제는 새 정보 없이 승격 금지]:**
`history`에서 `category == "skip"`인 항목만 추출. skip 이력이 있을 때만 이 섹션이 생성된다:
```
- "{topic_cluster}" -> {reason}
```

**[새로 수집된 기사]:**
`articles` 리스트를 1-based 번호로 포맷:
```
1. [{publisher}] {title}
   본문(1~2문단): {body}
   시각: {pubDate}
```

**분석 지시:**
```
각 기사에 대해:
1. 중복 제거: 동일 배치 내 병합 + 이전 보고 대비 중복 판단
2. [단독] 식별: 제목 태그 또는 사실상 단독 여부
3. 중복 아닌 기사에 주요도 판단 (A~D 기준 적용)
4. 보고 대상: 요약 + 해당되는 판단 근거 명시
```

### 1.4 submit_analysis 도구 스키마

```json
{
  "name": "submit_analysis",
  "description": "기사 분석 결과를 제출한다. 모든 기사를 results 또는 skipped에 빠짐없이 분류한다.",
  "input_schema": {
    "type": "object",
    "required": ["thinking", "results", "skipped"],
    "properties": {
      "thinking": {
        "type": "string",
        "description": "기사별 판단 과정. skip 시 해당 단계에서 끝, 전체 pass 시 s5까지 기록. 기사 구분은 |"
      },
      "results": {
        "type": "array",
        "description": "s1~s5 전체 통과한 기사 배열 (동일 사안은 대표 1건만, 나머지는 merged_indices에 기재)",
        "items": {
          "type": "object",
          "required": ["category", "topic_cluster", "source_indices", "merged_indices",
                       "title", "summary", "reason"],
          "properties": {
            "category": { "type": "string", "enum": ["exclusive", "important"] },
            "topic_cluster": { "type": "string", "description": "주제 식별자 (짧은 구문)" },
            "source_indices": { "type": "array", "items": { "type": "integer" },
                                "description": "대표 기사 번호" },
            "merged_indices": { "type": "array", "items": { "type": "integer" },
                                "description": "동일 사안으로 병합된 다른 기사 번호 (없으면 빈 배열)" },
            "title": { "type": "string" },
            "summary": { "type": "string", "description": "2~3문장 요약" },
            "reason": { "type": "string", "description": "판단 근거 1~2문장" }
          }
        }
      },
      "skipped": {
        "type": "array",
        "description": "s1~s5 중 하나라도 skip된 기사 배열 (병합으로 흡수된 기사는 여기에 넣지 않는다)",
        "items": {
          "type": "object",
          "required": ["topic_cluster", "source_indices", "title", "reason"],
          "properties": {
            "topic_cluster": { "type": "string", "description": "주제 식별자 (짧은 구문)" },
            "source_indices": { "type": "array", "items": { "type": "integer" },
                                "description": "대표 기사 번호" },
            "title": { "type": "string" },
            "reason": { "type": "string", "description": "스킵 사유" }
          }
        }
      }
    }
  }
}
```

### 1.5 중복 제거 로직 상세

check_agent의 중복 제거는 LLM에게 프롬프트로 위임하는 방식이다. 코드 수준의 사전 중복 제거는 없으며, 세 가지 축으로 LLM이 판단한다:

**같은 배치 내 중복 (topic_cluster 기반):**
시스템 프롬프트의 "[중복 제거 기준] 1번"에 의해 LLM이 동일 사안을 하나의 `topic_cluster`로 묶는다. 대표 1건은 `source_indices`에, 나머지는 `merged_indices`에 기재. 가장 포괄적인 기사를 대표로 선정한다.

**과거 이력 대비 중복:**
`_build_user_prompt`에서 `history` 중 보고 이력(`category != "skip"`)을 `[기자의 최근 보고 이력]` 섹션으로 전달한다. 각 이력의 `topic_cluster`와 `key_facts`를 함께 전달하여 LLM이 "동일 내용"인지 "새 팩트 추가"인지를 판단한다.

**skip 이력의 재등장 방지:**
`history` 중 `category == "skip"`인 항목을 별도 `[이전 skip 이력]` 섹션으로 분리 전달한다. 시스템 프롬프트의 "[이전 skip 기사 승격 제한]" 규칙과 결합하여, 이전 skip 주제가 새 정보 없이 승격되는 것을 방지한다.

---

## 2. report_agent.py -- 부서 뉴스 브리핑 에이전트

파일: `src/agents/report_agent.py`

`/report` 명령에서 호출되며, 2단계 LLM 파이프라인으로 당일 부서 관련 뉴스를 선별하고 브리핑을 생성한다.

### 2.1 Stage 1: filter_articles() -- Haiku 필터

```python
async def filter_articles(
    api_key: str,
    articles: list[dict],
    department: str,
) -> list[dict]:
```

**목적:** 본문 스크래핑 전 단계에서 제목+description만으로 부서 무관 기사를 사전 제거하여, 후속 스크래핑 및 Sonnet 분석의 입력량을 줄인다.

**모델 설정:**

| 항목 | 값 |
|---|---|
| 모델 | `claude-haiku-4-5-20251001` |
| Temperature | `0.0` |
| Max tokens | `2048` |
| tool_choice | `{"type": "tool", "name": "filter_news"}` (강제 도구 호출) |

**시스템 프롬프트 구성:**
인라인으로 조립한다. `DEPARTMENT_PROFILES`에서 `coverage`를 추출하여 다음 구조로 프롬프트를 구성:
```
당신은 {dept_label} 뉴스 필터입니다.
취재 영역: {coverage}

아래 기사 목록에서 다음 기준으로 기사 번호를 선별하세요:
1. 부서 관련성: 해당 부서 취재 영역에 해당하는 기사만 포함
2. 사진 캡션 제외: 본문 없이 사진 설명만 있는 포토뉴스 제외
3. 중복 사안 정리: 같은 사안의 다수 기사 중 대표 기사(최대 3건)만 선별
4. 애매한 경우 포함 쪽으로 판단

filter_news 도구로 선별된 기사 번호를 제출하세요.
```

**사용자 프롬프트 (기사 목록):**
`articles`를 다음 형식으로 포맷하여 전달:
```
[1] {언론사} | {제목} | {description}
[2] {언론사} | {제목} | {description}
...
```
언론사명은 `get_publisher_name(originallink)`으로 URL에서 추출한다.

**입력:** `search_news` 반환 형태 (title, description, originallink 등). 본문(body)은 아직 스크래핑 전이므로 포함되지 않는다.

**출력 파싱:** `filter_news` 도구 응답에서 `selected_indices`(1-based)를 추출하여 0-based로 변환 후 해당 기사만 반환한다. 범위 밖 인덱스는 무시한다. tool_use 응답이 없으면 전체 기사를 그대로 반환(fallback).

#### filter_news 도구 스키마

```json
{
  "name": "filter_news",
  "description": "부서 관련 기사 번호를 선별합니다",
  "input_schema": {
    "type": "object",
    "required": ["selected_indices"],
    "properties": {
      "selected_indices": {
        "type": "array",
        "items": { "type": "integer" },
        "description": "선별된 기사 번호 배열"
      }
    }
  }
}
```

### 2.2 Stage 2: analyze_report_articles() -- Haiku 분석

```python
async def analyze_report_articles(
    api_key: str,
    articles: list[dict],
    report_history: list[dict],
    existing_items: list[dict] | None,
    department: str,
) -> list[dict]:
```

**파라미터:**

| 파라미터 | 타입 | 설명 |
|---|---|---|
| `api_key` | `str` | Anthropic API 키 |
| `articles` | `list[dict]` | 수집된 기사 목록 (title, publisher, body, originallink, pubDate) |
| `report_history` | `list[dict]` | 최근 2일치 report_items 이력 |
| `existing_items` | `list[dict] \| None` | 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A) |
| `department` | `str` | 부서명 |

**모델 설정:**

| 항목 | 값 |
|---|---|
| 모델 | `claude-haiku-4-5-20251001` |
| Temperature | `0.0`~`0.4` (재시도마다 0.1씩 증가) |
| Max tokens | `16384` |
| tool_choice | `{"type": "tool", "name": "submit_report"}` (강제 도구 호출) |
| 재시도 | 최대 5회 (파싱 실패 시 temperature 점진적 증가) |

**시나리오 분기:** `existing_items`의 유무와 길이로 시나리오를 결정한다:
- 시나리오 A (`existing_items`가 None이거나 빈 리스트): 당일 첫 생성
- 시나리오 B (`existing_items`가 비어있지 않은 리스트): 기존 캐시 대비 업데이트

#### 2.2.1 시스템 프롬프트 구성 (_build_system_prompt)

```python
def _build_system_prompt(
    department: str,
    existing_items: list[dict] | None,
) -> str:
```

**공통 섹션 (시나리오 A/B 모두):**

1. **역할 정의:** `"당신은 {dept_label} 데스크의 뉴스 브리핑 보조입니다."`
2. **취재 영역:** `DEPARTMENT_PROFILES[dept_label]["coverage"]`
3. **중요 기사 판단 기준:** 공통 3개 기준 + 부서별 `criteria` 주입
   ```
   1) 복수 언론이 보도하거나, 단독 보도라면 팩트의 무게가 충분할 것
   2) 후속 보도 가능성이 높거나, 사안 자체가 사회적으로 중대할 것
   3) 아래 부서별 세부 기준에 해당할 것:
   - {criteria[0]}
   - {criteria[1]}
   ...
   ```
4. **단독 기사 식별:** check_agent와 동일한 식별 기준 (제목 태그, 취재에 따르면 패턴, 본문 어미)
5. **제외 기준:**
   - 단발성 사건/사고 (후속 보도 가능성 낮은 것)
   - 정례적 발표 (정부 보도자료, 정기 통계)
   - 단순 일정/예고
   - 관심도 낮은 사안 (소규모 지역 이슈)
   - 인터뷰/칼럼/사설
   - 재탕/종합 보도
   - 연예/스포츠 가십 (부서 무관)
6. **요약 작성 기준:** 2~3줄, 육하원칙 스트레이트 형식, 당일 팩트만, 구체적 팩트 필수, 사실 기반
7. **동일 사안 병합/분리 규칙:** 동일 사안 복수 보도는 source_indices로 묶어 1건으로, 단독 보도만 별도 분리
8. **중복/후속 판단 기준:** 육하원칙 핵심 사실이 동일하면 동일 뉴스. 새로운 앵글/관점/추가 디테일은 새 뉴스가 아님. follow_up은 단독 기사만 선정 가능

**시나리오 B 전용:**
기존 캐시 항목은 시스템 프롬프트가 아닌 사용자 프롬프트의 `[오늘 기존 항목]` 섹션에 포함된다.

**시나리오별 출력 규칙:**

시나리오 A (`[출력 규칙 - 첫 생성]`):
```
수집된 기사 중 부서 데스크가 반드시 알아야 할 사안만 엄선한다.
- 건수보다 품질이 우선. 기준에 미달하면 적게 선정해도 된다
- 이전 보고 이력을 참조하여 follow_up/new 분류
- 선택 사유(reason)를 명시 (왜 데스크가 알아야 하는지)
- source_indices로 참조 기사 번호를 기재 (URL 역매핑용)
- [단독] 태그 또는 특정 언론사만 보도한 기사는 exclusive: true
```

시나리오 B (`[출력 규칙 - 업데이트]`):
```
수집된 기사를 기존 캐시와 비교하여 변경/추가 사항만 보고한다.
- 기존 항목에 새 팩트가 추가됐으면 action: "modified" (기존 요약에 새 정보 병합)
- 기존 캐시에 없는 새로운 기사는 action: "added"
- 변경 없는 기존 항목은 출력하지 않음
- modified 항목은 item_id를 반드시 기재
- 추가 항목은 적극적으로 찾는다. 기존 캐시가 부족했을 수 있다
- 수정/추가 항목이 없으면 빈 배열을 제출
```

#### 2.2.2 사용자 프롬프트 구성 (_build_user_prompt)

```python
def _build_user_prompt(
    articles: list[dict],
    report_history: list[dict],
    existing_items: list[dict] | None,
) -> str:
```

사용자 프롬프트는 다음 섹션들을 `"\n\n"` 구분자로 연결한다.

**[이전 보고 이력 - 최근 2일]:**
`report_history`가 있으면 각 항목을 다음 형식으로 포맷:
```
- "{title}" | {summary} | key_facts: [{facts_str}] | {category} | {created_at[:10]}
```
이력이 없으면 `"이력 없음. 모든 항목을 category: "new", prev_reference: ""로 설정하라."` 명시.

**[오늘 기존 항목] (시나리오 B 전용):**
```
- 항목{seq} | {title} | 요약: {summary} | key_facts: [{facts_str}]
```
순번(`항목1`, `항목2`)은 DB id가 아닌 리스트 순서. handlers.py에서 순번→DB ID 변환 처리.

**[수집된 기사 목록]:**
```
1. [{publisher}] {title}
   본문: {body}
   시각: {pubDate}
```

**분석 지시:**
- 시나리오 A: `"위 기사를 분석하여 데스크가 주목할 사안을 선별하고 submit_report로 제출하시오."`
- 시나리오 B: `"위 기사를 기존 캐시와 비교하여 변경/추가 항목을 submit_report로 제출하시오."`

#### 2.2.3 submit_report 도구 스키마

도구 스키마는 `_build_report_tool(is_scenario_b)` 함수로 시나리오에 따라 동적으로 생성된다.

**최상위 필드:**
- `thinking` (string): 기사별 판단 과정 (step별 pass/skip 기록)
- `results` (array): 브리핑 항목 배열

**results 항목 공통 필드 (시나리오 A/B 모두):**
- `title` (string): 대표 기사의 원본 제목 (수집된 기사 목록의 제목을 그대로 사용)
- `source_indices` (array[integer]): 참조 기사 번호 배열
- `merged_indices` (array[integer]): 동일 사안으로 병합된 다른 기사 번호 (없으면 빈 배열)
- `summary` (string): 2~3줄 육하원칙 스트레이트 형식 요약
- `reason` (string): 포함 사유 1~2문장
- `exclusive` (boolean): [단독] 여부

**시나리오 B 추가 필드:**
- `action` (string, enum: ["modified", "added"]): 기존 캐시 대비 변경 유형
- `item_id` (integer): 수정 대상 기존 항목 순번 (modified일 때 해당 순번, added일 때 0)

**스키마에서 제거되어 `_parse_report_response()`에서 기본값 주입되는 필드:**
- `category`: 스키마에 없음. 파싱 시 `"new"` 기본값 주입
- `prev_reference`: 스키마에 없음. 파싱 시 `""` 기본값 주입

**설계 의도:**
- `url` 필드 제거: URL은 `source_indices`를 통해 handlers.py의 `_map_results_to_articles()`에서 역매핑
- `category`/`prev_reference` 제거: LLM에게 분류 부담을 줄이고, `_parse_report_response()`에서 DB 호환 기본값을 주입
- 시나리오 A에서는 `action`/`item_id` 필드 자체가 스키마에 포함되지 않음

---

## 3. 부서 프로필 (DEPARTMENT_PROFILES)

파일: `src/config.py`

8개 부서 각각에 대해 다음 3개 필드를 정의한다.

### 3.1 프로필 구조

| 필드 | 타입 | 설명 |
|---|---|---|
| `coverage` | `str` | 부서의 취재 영역을 쉼표로 나열한 텍스트 |
| `criteria` | `list[str]` | 중요도 판단 기준 (각 항목이 "공식 수사 조치: 체포, 구속..." 형태) |
| `report_keywords` | `list[str]` | `/report` 브리핑용 검색 키워드 |

### 3.2 부서별 프로필

**사회부:**
- coverage: 사건/사고, 경찰/검찰 수사, 법원 재판, 인권, 재난/안전, 교육, 노동, 환경, 지방자치, 부동산/주거, 의료/보건/복지, 인구/저출생/고령화, 소비자/생활경제
- criteria: 공식 수사 조치, 중대 사건/사고, 수사 국면 전환, 사회적 파장, 사회 구조적 이슈 (5개)
- report_keywords: 13개

**정치부:**
- coverage: 국회 입법, 정당 동향, 대통령실/행정부, 선거, 외교/안보, 남북관계, 헌법재판소, 검찰/법무(정치적 측면), 여론/정치 지형 변화
- criteria: 입법 조치, 행정부 결정, 정당 동향, 외교/안보 (4개)
- report_keywords: 12개

**경제부:**
- coverage: 거시경제, 금융/통화 정책, 부동산, 세제/재정, 금융감독, 가계/소비 지표, 가상자산/핀테크, 국제경제, 물가/생활경제
- criteria: 정책 결정, 주요 지표, 금융 이슈, 규제/감독, 국제경제 (5개)
- report_keywords: 13개

**산업부:**
- coverage: 대기업 그룹(삼성/LG/SK/현대/롯데/한화/포스코/GS), 중소/중견기업, 통신, 에너지/전력, 유통/물류, 건설/인프라, ESG/탄소중립, 통상/무역
- criteria: 대기업 주요 결정, 산업 정책, 시장 판도 변화, 에너지/인프라 (5개, 제외 대상 포함)
- report_keywords: 12개

**테크부:**
- coverage: 반도체/디스플레이, 배터리/2차전지, 자동차/모빌리티, AI/로봇, 바이오/제약, 스타트업/벤처, 핀테크, 클라우드/데이터센터
- criteria: 기술 패권, 기업 주요 결정, 혁신, 스타트업, 바이오 (6개, 제외 대상 포함)
- report_keywords: 12개

**문화부:**
- coverage: 문화/예술, 방송/미디어, 연예, 출판, 관광, 종교, 교육 정책, 문화재, 게임, 웹툰/웹소설, 한류/글로벌 콘텐츠, OTT/플랫폼
- criteria: 수상/선정, 정책/제도, 업계 이슈, 사회적 관심 (4개)
- report_keywords: 12개

**스포츠부:**
- coverage: 프로 스포츠(야구/축구/농구/배구), 국제 대회, 올림픽, e스포츠, 체육 행정
- criteria: 경기 결과, 선수 동향, 대회/행사, 체육 행정 (4개)
- report_keywords: 11개

**국제부:**
- coverage: 국제 정치/외교, 글로벌 경제/통상, 주요국 정책(미국/중국/유럽/일본), 국제 분쟁/안보, 글로벌 테크/산업, 국제기구(UN/WHO/IMF 등), 글로벌 문화/사회
- criteria: 주요국 정책, 국제 분쟁/안보, 글로벌 경제, 글로벌 테크, 국제기구 (5개)
- report_keywords: 12개

### 3.3 프로필의 프롬프트 주입 경로

**check_agent:**
- `_build_system_prompt`에서 `coverage` --> `{coverage_section}`, `criteria` --> `{criteria_section}`으로 시스템 프롬프트 템플릿에 주입
- `report_keywords`는 check_agent에서 직접 사용하지 않음 (기자의 개인 키워드를 별도 수신)

**report_agent:**
- `filter_articles`에서 `coverage`만 시스템 프롬프트에 주입 (Haiku 필터)
- `_build_system_prompt`에서 `coverage`와 `criteria`를 시스템 프롬프트에 주입 (Sonnet 분석)
- `report_keywords`는 기사 수집 단계에서 검색 쿼리로 사용 (에이전트 외부)

---

## 4. 분류 체계 총정리

### 4.1 /check 분류

| 분류 | 값 | 판단 기준 |
|---|---|---|
| 단독 | `exclusive` | 제목에 `[단독]` 태그가 있거나, 본문에 "취재에 따르면" 패턴이 있는 자체 취재 기사. 최우선 선정 대상 |
| 주요 | `important` | 키워드 관련성을 통과하고, 부서별 criteria + 경쟁 관점/사회적 맥락/시의성 기준에 부합하는 기사 |
| 스킵 | `skip` | 키워드 무관, 이전 보고와 동일 내용, 이전 skip 주제 재등장(새 정보 없음), 또는 주요도 기준 미달 |

단독 기사 식별 조건:
1. 제목에 `[단독]` 태그 --> 무조건 선정
2. 본문에 "OO 취재에 따르면", "본지 취재 결과" 등 자체 취재 패턴 --> 사실상 단독
3. 본문 어미 보조 판단: "확인됐다" > "드러났다" > "전해졌다" 순으로 뉴스 가치

### 4.2 /report 분류

| 분류 | 값 | 판단 기준 |
|---|---|---|
| 후속 | `follow_up` | 이전 2일치 보고 이력에 관련 사안이 존재하는 경우. `prev_reference`에 "YYYY-MM-DD \"이전 제목\"" 형식으로 참조 기재 |
| 신규 | `new` | 이전 보고 이력에 없는 새로운 사안. `prev_reference`는 null |

추가로 `exclusive: boolean` 플래그로 단독 기사 여부를 별도 표시한다.

### 4.3 배제 기준 (/report 전용)

/report의 시스템 프롬프트에 명시된 제외 기준:
- 단발성 사건/사고 (후속 보도 가능성 낮은 개별 사망, 교통사고, 화재)
- 정례적 발표 (정부 보도자료, 정기 통계, 일상적 권고/캠페인)
- 단순 일정/예고 (행사 안내, 연휴 당부, 날씨 전망)
- 관심도 낮은 사안 (소규모 지역 이슈, 단일 기관 내부 사안)
- 인터뷰/칼럼/사설
- 재탕/종합 보도 (이미 알려진 팩트 재구성, 타 매체 인용 정리)
- 연예/스포츠 가십 (부서 취재 영역과 무관)

---

## 5. 토큰 사용량 최적화 설계

### 5.1 /report의 2단 파이프라인 (Haiku 필터 → Haiku 분석)

| 단계 | 모델 | 입력 | 비용 특성 |
|---|---|---|---|
| Stage 1 | Haiku (`claude-haiku-4-5-20251001`) | 제목 + description (본문 없음) | 저비용 모델, 경량 입력 |
| Stage 2 | Haiku (`claude-haiku-4-5-20251001`) | 필터 통과 기사의 제목 + 본문 | 저비용 모델, 필터링된 입력 |

/check도 2단 Haiku 파이프라인으로 동작한다: Haiku 사전 필터 → Haiku 분석.

Stage 1에서 부서 무관 기사, 사진 캡션, 중복 사안을 제거하므로 Stage 2에 전달되는 기사 수가 줄어든다. Stage 1은 본문 스크래핑 전에 실행되므로 스크래핑 자체도 필터 통과 기사에 대해서만 수행한다. 이 구조로 분석 에이전트에 전달되는 입력 토큰과 스크래핑 비용을 동시에 절감한다.

### 5.2 본문 800자 제한

기사 본문은 스크래핑 시 최대 800자만 추출한다 (`_MAX_CHARS = 800`). 문단 수가 아닌 글자 수 기반으로 제한하며, LLM에 전달되는 입력 토큰을 통제하면서도 기사의 핵심 정보를 충분히 파악할 수 있는 분량이다.

### 5.3 Temperature (0.0~0.4 점진적 증가)

check_agent와 report_agent 모두 첫 시도에서 Temperature 0.0으로 시작하여 결정론적 출력을 유도한다. 파싱 실패 시 재시도마다 0.1씩 증가시켜(최대 0.4) 동일 실패 패턴을 회피한다. 5회 시도 후에도 파싱 실패 시 `RuntimeError`를 발생시킨다.

### 5.4 강제 도구 호출 (tool_choice)

세 LLM 호출 모두 `tool_choice`를 특정 도구로 지정한다:
- check_agent: `{"type": "tool", "name": "submit_analysis"}`
- report_agent 필터: `{"type": "tool", "name": "filter_news"}`
- report_agent 분석: `{"type": "tool", "name": "submit_report"}`

이 방식으로 LLM이 자연어 응답 대신 반드시 구조화된 JSON을 출력하게 하여 파싱 실패를 방지한다.

---

## 6. Langfuse 트레이싱

세 LLM 호출 모두 `langfuse.start_as_current_observation` 컨텍스트 매니저로 트레이싱된다. `langfuse` 인스턴스는 `from langfuse import get_client as get_langfuse`로 가져온다.

### 6.1 트레이싱 위치 및 설정

| 에이전트 | 함수 | observation 이름 | as_type | metadata |
|---|---|---|---|---|
| check_agent | `filter_check_articles` | `"check_filter"` | `"span"` | `{"department": department, "input_count": len(articles)}` |
| check_agent | `analyze_articles` | `"check_agent"` | `"span"` | `{"department": department, "attempt": attempt + 1}` |
| report_agent | `filter_articles` | `"report_filter"` | `"span"` | `{"department": department, "input_count": len(articles)}` |
| report_agent | `analyze_report_articles` | `"report_agent"` | `"span"` | `{"department": department, "scenario": scenario, "attempt": attempt + 1}` |

### 6.2 추적되는 정보

- `check_filter`: 부서명 + 필터 입력 기사 수
- `check_agent`: 부서명 + 시도 횟수
- `report_filter`: 부서명 + 필터 입력 기사 수
- `report_agent`: 부서명 + 시나리오 (A 또는 B) + 시도 횟수

Claude API 호출은 `start_as_current_observation` 블록 내에서 실행되므로, Langfuse SDK가 자동으로 해당 span 하위에 LLM generation을 기록한다. 응답의 `input_tokens`, `output_tokens`, `stop_reason`은 별도로 로거에 기록된다.
