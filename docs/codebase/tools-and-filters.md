# Tools & Filters 상세 문서

이 문서는 tasa-check 프로젝트의 뉴스 수집(search), 본문 스크래핑(scraper), 언론사 필터링(publisher) 모듈의 구현을 코드 레벨에서 해부한다.

---

## 1. search.py -- 네이버 뉴스 검색 API 모듈

파일 경로: `src/tools/search.py`

네이버 오픈 API를 사용하여 키워드별 뉴스 기사를 검색하고, 시간 필터링과 중복 제거를 거쳐 정제된 기사 리스트를 반환한다.

### 1.1. 모듈 상수

| 상수명 | 값 | 설명 |
|---|---|---|
| `_SEARCH_URL` | `https://openapi.naver.com/v1/search/news.json` | 네이버 뉴스 검색 API 엔드포인트 |
| `_DISPLAY` | `100` | 한 페이지당 요청 건수 (API 최대값) |
| `_MAX_PAGES` | `2` | 키워드당 최대 페이지 수 |
| `_MAX_TOTAL_RESULTS` | `200` | check 기본 최대 결과 수 |
| `_BATCH_SIZE` | `3` | 동시 요청 키워드 배치 크기 |
| `_BATCH_DELAY` | `0.5` | 배치 간 대기 시간 (초) |
| `_RETRY_MAX` | `2` | 429 에러 시 최대 재시도 횟수 |
| `_RETRY_DELAY` | `1.0` | 429 재시도 기본 대기 시간 (초) |
| `_HTML_TAG_RE` | `re.compile(r"<[^>]+>")` | HTML 태그 제거용 정규식 |

### 1.2. search_news() -- 메인 검색 함수

```python
async def search_news(
    keywords: list[str],
    since: datetime,
    max_results: int = _MAX_TOTAL_RESULTS,
) -> list[dict]:
```

**파라미터:**
- `keywords` -- 검색 키워드 리스트. 각 키워드별로 개별 API 호출을 수행한다.
- `since` -- 이 시각 이후에 발행된 기사만 포함한다.
- `max_results` -- 반환할 최대 기사 수. 기본값 `200`(check), report 호출 시 `400`을 전달한다.

**호출 지점별 max_results:**
- `/check` 명령: 기본값 200건 (`src/bot/handlers.py` 54행)
- `/report` 명령: 400건 명시 전달 (`src/bot/handlers.py` 143행)

**실행 흐름:**

1. 네이버 API 인증 헤더를 구성한다 (`X-Naver-Client-Id`, `X-Naver-Client-Secret`).
2. 키워드를 `_BATCH_SIZE`(3)개씩 묶어 배치로 나눈다.
3. 각 배치 내 키워드들을 `asyncio.gather()`로 병렬 실행한다.
4. 마지막 배치가 아니면 `_BATCH_DELAY`(0.5초) 대기 후 다음 배치로 진행한다.
5. 전체 결과를 `originallink` URL 기준으로 중복 제거한다.
6. `pubDate` 내림차순(최신순)으로 정렬한다.
7. `max_results`건까지 잘라서 반환한다.

**URL 기반 중복 제거 로직:**

```python
seen_urls: set[str] = set()
results: list[dict] = []
for keyword_results in all_results:
    for article in keyword_results:
        url = article["originallink"]
        if url not in seen_urls:
            seen_urls.add(url)
            results.append(article)
```

서로 다른 키워드로 검색했을 때 동일한 기사가 중복으로 수집될 수 있다. `originallink`(기사 원문 URL)를 기준으로 먼저 수집된 것만 남긴다.

### 1.3. _search_keyword() -- 단일 키워드 검색

```python
async def _search_keyword(
    client: httpx.AsyncClient,
    keyword: str,
    since: datetime,
    headers: dict,
) -> list[dict]:
```

단일 키워드에 대해 네이버 뉴스 API를 호출하여 `since` 이후 기사를 수집한다.

**API 요청 파라미터:**

```python
params = {
    "query": keyword,
    "display": 100,        # 한 페이지 결과 수
    "start": 1,            # 검색 시작 위치 (1페이지: 1, 2페이지: 101)
    "sort": "date",        # 날짜순 정렬
}
```

**페이지네이션:**
- 최대 `_MAX_PAGES`(2) 페이지를 순회한다.
- 1페이지: `start=1`, 2페이지: `start=101`
- 페이지당 `_DISPLAY`(100)건씩, 키워드당 최대 200건까지 수집 가능하다.

**조기 종료 조건 (3가지):**
1. `_request_with_retry()`가 `None`을 반환한 경우 (API 에러)
2. 응답 items가 비어있는 경우
3. `since`보다 오래된 기사가 발견된 경우 (`hit_old = True`)
4. 응답 items 수가 `_DISPLAY` 미만인 경우 (더 이상 결과 없음)

각 아이템은 `_parse_item()`으로 정제한 뒤, `pubDate >= since` 조건을 만족하는 것만 결과에 추가한다.

### 1.4. _request_with_retry() -- 재시도 포함 HTTP 요청

```python
async def _request_with_retry(
    client: httpx.AsyncClient,
    headers: dict,
    params: dict,
) -> dict | None:
```

네이버 API 요청을 수행하며, HTTP 429 (Rate Limit) 응답 시 지수 백오프로 재시도한다.

**재시도 로직:**
- 최대 재시도 횟수: `_RETRY_MAX`(2)회
- 대기 시간: `_RETRY_DELAY * (attempt + 1)` -- 1차 재시도 1초, 2차 재시도 2초
- 재시도 한도를 초과하면 `None`을 반환하고 해당 키워드를 건너뛴다.
- 429 이외의 HTTP 에러는 `resp.raise_for_status()`로 예외를 발생시킨다.

**반환값:**
- 성공 시: API 응답 JSON (dict)
- 실패 시: `None`

### 1.5. _parse_item() -- API 응답 아이템 파싱

```python
def _parse_item(item: dict) -> dict:
```

네이버 API 응답의 개별 아이템을 정제된 dict로 변환한다.

**반환 데이터 구조:**

```python
{
    "title": "검찰, 전 장관 구속영장 청구",       # HTML 태그 제거된 제목
    "link": "https://n.news.naver.com/...",       # 네이버 뉴스 링크
    "originallink": "https://www.chosun.com/...", # 기사 원문 URL
    "description": "검찰이 전 장관에 대해...",     # HTML 태그 제거된 요약
    "pubDate": datetime(2026, 2, 13, 14, 30, ...),  # datetime 객체
}
```

**처리 내용:**
- `title`, `description`: `_strip_html()`으로 `<b>`, `</b>` 등 HTML 태그를 제거한다.
- `pubDate`: `_parse_pub_date()`로 RFC 2822 형식 문자열(`"Thu, 13 Feb 2026 14:30:00 +0900"`)을 `datetime` 객체로 변환한다. 내부적으로 `email.utils.parsedate_to_datetime()`을 사용한다.
- `link`, `originallink`: API 응답 값을 그대로 전달한다.

### 1.6. 배치 처리 흐름 요약

키워드가 `["경찰 수사", "검찰 기소", "법원 판결", "사건사고", "재난 안전"]` (5개)일 때:

1. **배치 1**: `["경찰 수사", "검찰 기소", "법원 판결"]` -- `asyncio.gather()`로 3개 병렬 실행
2. 0.5초 대기
3. **배치 2**: `["사건사고", "재난 안전"]` -- `asyncio.gather()`로 2개 병렬 실행 (마지막 배치이므로 대기 없음)

---

## 2. scraper.py -- 네이버 뉴스 기사 본문 스크래퍼

파일 경로: `src/tools/scraper.py`

네이버 뉴스(`n.news.naver.com`) 기사 페이지에서 본문 첫 1~3문단을 추출한다. Claude 분석 시 context 절약을 위해 전체 본문이 아닌 앞부분만 가져온다.

### 2.1. 모듈 상수

| 상수명 | 값 | 설명 |
|---|---|---|
| `_TIMEOUT` | `10.0` | HTTP 요청 타임아웃 (초) |
| `_HEADERS` | Chrome UA 문자열 | User-Agent 위장 헤더 |
| `_MAX_PARAGRAPHS` | `3` | 추출할 최대 문단 수 |
| `_SUBHEADING_MARKERS` | `set("▶■◆●△▷▲►◇□★☆※➤")` | 소제목 판별용 특수 기호 집합 |
| `_scrape_semaphore` | `asyncio.Semaphore(50)` | 전역 동시 스크래핑 제한 |

**User-Agent 헤더:**

```python
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}
```

### 2.2. fetch_articles_batch() -- 배치 스크래핑 진입점

```python
async def fetch_articles_batch(urls: list[str]) -> dict[str, str | None]:
```

여러 URL의 기사 본문을 병렬로 가져온다.

**반환값:**
- `dict[str, str | None]` -- URL을 키, 본문 텍스트(또는 `None`)를 값으로 하는 딕셔너리

**구현 상세:**
- `httpx.AsyncClient`를 하나 생성하여 모든 요청에서 커넥션 풀을 공유한다.
- 내부 함수 `_fetch_one()`에서 전역 세마포어 `_scrape_semaphore`(50)를 acquire하여 동시 요청 수를 제한한다.
- `asyncio.gather()`로 모든 URL을 병렬 실행한다.
- 개별 요청 실패 시 해당 URL의 결과를 `None`으로 반환한다 (graceful degradation). 다른 URL 처리에는 영향을 주지 않는다.
- 완료 후 성공 건수를 로깅한다.

**실행 흐름:**

```
urls 리스트 입력
  -> httpx.AsyncClient 생성 (timeout=10초, User-Agent 헤더, follow_redirects=True)
    -> URL마다 _fetch_one() 태스크 생성
      -> _scrape_semaphore.acquire() (동시 50개 제한)
        -> client.get(url)
        -> _parse_article_body(resp.text)
      -> _scrape_semaphore.release()
    -> asyncio.gather()로 전체 병렬 실행
  -> {url: body} 딕셔너리 반환
```

### 2.3. fetch_article_body() -- 단일 기사 스크래핑

```python
async def fetch_article_body(url: str) -> str | None:
```

단일 URL에서 기사 본문을 가져온다. `fetch_articles_batch()`와 달리 독립적인 `httpx.AsyncClient`를 생성한다. 세마포어를 사용하지 않는다.

**에러 처리:** 네트워크 오류, HTTP 오류, 파싱 실패 등 모든 예외를 `except Exception`으로 캐치하여 `None`을 반환하고 경고 로그를 남긴다.

### 2.4. _parse_article_body() -- HTML 본문 파싱

```python
def _parse_article_body(html: str) -> str | None:
```

네이버 뉴스 기사 HTML에서 본문 첫 3문단을 추출한다.

**파싱 단계:**

**1단계 -- 본문 컨테이너 탐색:**

```python
container = soup.select_one("article#dic_area") or soup.select_one("div#newsct_article")
```

- 1차 시도: `article#dic_area` (네이버 뉴스 표준 기사 컨테이너)
- 2차 시도: `div#newsct_article` (폴백 컨테이너)
- 둘 다 없으면 `None` 반환

**2단계 -- `<p>` 태그에서 문단 추출:**

컨테이너 내 모든 `<p>` 태그를 순회하며 다음 필터를 적용한다:

- **사진 캡션 필터링**: `<p>` 태그의 상위 요소 중 class에 `"photo"`, `"img"`, `"vod"` 문자열이 포함된 것이 있으면 건너뛴다.
- **빈 텍스트 필터링**: `get_text(strip=True)` 결과가 빈 문자열이면 건너뛴다.
- **소제목 필터링**: `_is_subheading()` 판별 결과가 `True`이면 건너뛴다.
- `_MAX_PARAGRAPHS`(3)개에 도달하면 순회를 중단한다.

**3단계 -- raw text 폴백:**

`<p>` 태그에서 문단을 하나도 추출하지 못한 경우, 컨테이너의 전체 텍스트를 줄바꿈(`\n`) 기준으로 분리하여 동일한 소제목 필터링을 적용한다.

**4단계 -- 결과 조합:**

추출된 문단들을 `"\n".join(paragraphs)`으로 합쳐 반환한다. 문단이 없으면 `None`을 반환한다.

### 2.5. _is_subheading() -- 소제목 판별

```python
def _is_subheading(text: str, tag=None) -> bool:
```

텍스트가 소제목인지 판별한다. 소제목은 뉴스 본문의 섹션 구분 용도로 사용되며, 본문 추출 시 제외 대상이다.

**판별 조건 (하나라도 해당하면 소제목):**

1. `text`가 빈 문자열이면 `True` (빈 텍스트를 소제목으로 간주하여 스킵)
2. 첫 글자가 `_SUBHEADING_MARKERS` 집합에 포함되면 `True`
   - 대상 기호: `▶ ■ ◆ ● △ ▷ ▲ ► ◇ □ ★ ☆ ※ ➤`
3. `tag` 파라미터가 전달되고, 텍스트 길이가 50자 미만이며, 해당 태그 내에 `<b>` 또는 `<strong>` 태그가 있고 그 볼드 텍스트가 전체 텍스트와 일치하면 `True`

3번 조건은 기사에서 흔히 볼 수 있는 "전체가 볼드 처리된 짧은 소제목" 패턴을 감지한다.

---

## 3. publisher.py -- 언론사 화이트리스트 필터 모듈

파일 경로: `src/filters/publisher.py`

`data/publishers.json`에 정의된 언론사 화이트리스트를 기반으로 기사를 필터링한다. 화이트리스트에 없는 언론사의 기사는 제거된다.

### 3.1. load_publishers() -- 언론사 목록 로드

```python
@lru_cache(maxsize=1)
def load_publishers() -> list[dict]:
```

`data/publishers.json` 파일을 읽어 `publishers` 배열을 반환한다. `@lru_cache(maxsize=1)`로 캐싱하여 프로세스 수명 동안 한 번만 파일을 읽는다.

**파일 경로 결정:**

```python
_PUBLISHERS_PATH = BASE_DIR / "data" / "publishers.json"
```

`BASE_DIR`은 `src/config.py`에서 `Path(__file__).resolve().parent.parent`로 정의되며, 프로젝트 루트 디렉토리를 가리킨다.

### 3.2. filter_by_publisher() -- 기사 필터링

```python
def filter_by_publisher(articles: list[dict]) -> list[dict]:
```

기사 리스트를 받아 화이트리스트에 포함된 언론사의 기사만 반환한다.

**동작 방식:**

1. `load_publishers()`로 화이트리스트를 가져온다.
2. 각 기사의 `originallink` 필드에서 도메인을 추출한다.
3. 추출된 도메인을 화이트리스트의 모든 언론사 도메인과 `_match_domain()`으로 비교한다.
4. 매칭되는 언론사가 있으면 결과 리스트에 추가한다.

**입력:** `search_news()`가 반환한 기사 dict 리스트
**출력:** 화이트리스트 매칭된 기사 dict 리스트 (원본 dict를 그대로 포함, 별도 필드 추가 없음)

참고로, `publisher_name` 필드 추가는 이 함수가 아닌 `src/bot/handlers.py`에서 `get_publisher_name()`을 별도로 호출하여 수행한다.

### 3.3. get_publisher_name() -- 언론사명 조회

```python
def get_publisher_name(url: str) -> str | None:
```

URL에 해당하는 언론사 이름을 반환한다. 화이트리스트에 없으면 `None`.

**사용처:**
- `src/bot/handlers.py` -- check/report 분석용 기사 데이터 구성 시 `publisher` 필드에 언론사명을 넣기 위해 호출
- `src/agents/report_agent.py` -- report 분석 시 기사별 언론사명 조회

### 3.4. _match_domain() -- 도메인 매칭

```python
def _match_domain(article_domain: str, publisher_domain: str) -> bool:
```

기사 도메인이 언론사 도메인에 속하는지 판별한다.

**매칭 규칙:**

```python
return (
    article_domain == publisher_domain
    or article_domain.endswith("." + publisher_domain)
)
```

1. 정확히 일치하는 경우: `"chosun.com" == "chosun.com"` -- 매칭
2. 서브도메인인 경우: `"news.chosun.com".endswith(".chosun.com")` -- 매칭
3. 단순 문자열 포함이 아닌 `.` 구분자 기반이므로: `"notchosun.com".endswith(".chosun.com")` -- 매칭 안됨

### 3.5. _extract_domain() -- URL에서 도메인 추출

```python
def _extract_domain(url: str) -> str | None:
```

`urllib.parse.urlparse()`를 사용하여 URL에서 호스트명을 추출한다. 파싱 실패 시 `None`을 반환한다.

---

## 4. config.py -- 관련 설정값

파일 경로: `src/config.py`

### 4.1. API 인증 정보

```python
NAVER_CLIENT_ID: str = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET: str = os.environ["NAVER_CLIENT_SECRET"]
```

`.env` 파일 또는 환경 변수에서 로드한다. `search.py`에서 네이버 뉴스 검색 API 인증 헤더에 사용된다.

### 4.2. 시간 윈도우 설정

| 상수명 | 값 | 설명 |
|---|---|---|
| `CHECK_MAX_WINDOW_SECONDS` | `10800` (3시간) | /check 명령 시 수집 대상 시간 범위 최대값 |
| `REPORT_MAX_WINDOW_SECONDS` | `10800` (3시간) | /report 명령 시 수집 대상 시간 범위 |

두 값 모두 `3 * 60 * 60 = 10800`초(3시간)로 동일하다. `search_news()`의 `since` 파라미터 계산에 사용된다.

### 4.3. 기타 상수

| 상수명 | 값 | 설명 |
|---|---|---|
| `BASE_DIR` | `Path(__file__).resolve().parent.parent` | 프로젝트 루트 디렉토리 경로 |
| `CACHE_RETENTION_DAYS` | `5` | 캐시 보관 기간 (일) |

### 4.4. DEPARTMENT_PROFILES의 report_keywords

`DEPARTMENT_PROFILES`는 부서별 프로필을 정의하며, 각 부서에 `report_keywords` 리스트가 포함되어 있다. 이 키워드가 `/report` 명령 시 `search_news()`에 전달된다.

| 부서 | report_keywords 수 | 주요 키워드 예시 |
|---|---|---|
| 사회부 | 13개 | 경찰 수사, 검찰 기소, 법원 판결, 부동산 정책, 의료 보건 등 |
| 정치부 | 12개 | 국회, 대통령실, 여당, 야당, 외교, 헌법재판소 등 |
| 경제부 | 13개 | 기준금리, 부동산 규제, 물가 상승, 수출 실적, GDP 등 |
| 산업부 | 13개 | 반도체, 배터리, AI 인공지능, 대기업 실적, M&A 인수합병 등 |
| 문화부 | 12개 | 영화 흥행, 드라마, 방송, OTT, 한류, K팝 아이돌 등 |
| 스포츠부 | 11개 | 프로야구, 축구 대표팀, 올림픽, FA 이적, 월드컵 등 |

---

## 5. publishers.json -- 언론사 화이트리스트

파일 경로: `data/publishers.json`

### 5.1. JSON 구조

```json
{
  "description": "주요 언론사 화이트리스트 (27개)",
  "publishers": [
    {
      "name": "조선일보",
      "domain": "chosun.com",
      "category": "종합일간지"
    }
  ]
}
```

각 언론사 엔트리의 필드:
- `name` -- 언론사 한글 이름 (get_publisher_name 반환값)
- `domain` -- 도메인 매칭용 문자열 (_match_domain에서 사용)
- `category` -- 분류 (현재 코드에서 사용하지 않음, 참조용)

### 5.2. 언론사 목록 (총 27개)

**종합일간지 (7개)**

| 언론사 | 도메인 |
|---|---|
| 조선일보 | `chosun.com` |
| 중앙일보 | `joongang.co.kr` |
| 동아일보 | `donga.com` |
| 한겨레 | `hani.co.kr` |
| 경향신문 | `khan.co.kr` |
| 한국일보 | `hankookilbo.com` |
| 국민일보 | `kmib.co.kr` |

**석간 (3개)**

| 언론사 | 도메인 |
|---|---|
| 문화일보 | `munhwa.com` |
| 서울신문 | `seoul.co.kr` |
| 세계일보 | `segye.com` |

**경제지 (6개)**

| 언론사 | 도메인 |
|---|---|
| 매일경제 | `mk.co.kr` |
| 한국경제 | `hankyung.com` |
| 머니투데이 | `mt.co.kr` |
| 헤럴드경제 | `heraldcorp.com` |
| 아시아경제 | `asiae.co.kr` |
| 이데일리 | `edaily.co.kr` |

**지상파 (3개)**

| 언론사 | 도메인 |
|---|---|
| KBS | `news.kbs.co.kr` |
| MBC | `imnews.imbc.com` |
| SBS | `news.sbs.co.kr` |

**종편 (4개)**

| 언론사 | 도메인 |
|---|---|
| JTBC | `news.jtbc.co.kr` |
| 채널A | `ichannela.com` |
| TV조선 | `tvchosun.com` |
| MBN | `mbn.co.kr` |

**보도전문 (1개)**

| 언론사 | 도메인 |
|---|---|
| YTN | `ytn.co.kr` |

**통신사 (3개)**

| 언론사 | 도메인 |
|---|---|
| 연합뉴스 | `yna.co.kr` |
| 뉴시스 | `newsis.com` |
| 뉴스1 | `news1.kr` |

### 5.3. 도메인 패턴

화이트리스트의 도메인은 두 가지 형태가 혼재한다:

- **루트 도메인**: `chosun.com`, `donga.com` 등 -- 해당 도메인의 모든 서브도메인(`news.chosun.com` 등)도 매칭된다.
- **서브도메인**: `news.kbs.co.kr`, `imnews.imbc.com` 등 -- 정확히 해당 서브도메인(또는 그 하위)만 매칭된다. 예를 들어 `news.kbs.co.kr`은 매칭되지만 `kbs.co.kr`은 매칭되지 않는다.

---

## 6. 전체 파이프라인 요약

```
[키워드]
  |
  v
search_news()  -- 네이버 API로 기사 수집 (check: 200건, report: 400건)
  |
  v
filter_by_publisher()  -- 화이트리스트 27개 언론사 필터링
  |
  v
fetch_articles_batch()  -- 네이버 뉴스 링크로 본문 1~3문단 스크래핑
  |
  v
get_publisher_name()  -- 각 기사에 언론사명 부여
  |
  v
[분석 에이전트로 전달]
```
