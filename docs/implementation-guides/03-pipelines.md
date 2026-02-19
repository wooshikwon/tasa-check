# C: Pipelines 구현 가이드

> 담당: handlers.py의 파이프라인 로직을 독립 모듈로 추출

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/pipelines/__init__.py` | 신규 생성 — 공유 헬퍼 함수 |
| `src/pipelines/check.py` | 신규 생성 — check 파이프라인 |
| `src/pipelines/report.py` | 신규 생성 — report 파이프라인 |

## 금지 사항

- `src/bot/handlers.py` 수정 금지 (읽기만 허용, 코드 복사용)
- 다른 파일 수정 금지
- 파이프라인 로직 변경 금지 (기존 동작 100% 보존)

---

## 핵심 원칙

handlers.py에서 파이프라인 로직을 **복사**하여 새 파일로 분리한다.
handlers.py 자체는 수정하지 않는다 (Integration 에이전트가 나중에 처리).
기존 로직을 1:1로 옮기되, 함수 시그니처만 정리한다.

---

## 노출 인터페이스

```python
# src/pipelines/check.py
async def run_check(db, journalist: dict) -> tuple[list[dict] | None, datetime, datetime, int]
# 반환: (results, since, now, haiku_filtered_count)

# src/pipelines/report.py
async def run_report(
    db, journalist: dict, existing_items: list[dict] | None = None,
) -> list[dict] | None
```

---

## 상세 구현

### 현재 handlers.py 구조 파악

handlers.py에서 추출 대상 함수:

1. **`_normalize_title(title: str) -> str`** — 제목 정규화 (HTML 태그 제거, 공백 정리)
2. **`_match_article(llm_title: str, articles: list[dict]) -> dict | None`** — LLM 출력 제목 → 원본 기사 매칭
3. **`_map_results_to_articles(results, articles, url_key="url") -> None`** — source_indices → URL/publisher/pub_time 매핑
4. **`_run_check_pipeline(db, journalist) -> tuple`** — 체크 파이프라인 전체
5. **`_run_report_pipeline(db, journalist, existing_items=None) -> list | None`** — 리포트 파이프라인 전체

이 5개 함수를 pipelines/ 모듈로 이동한다.

### 1. src/pipelines/__init__.py

```python
"""파이프라인 공유 헬퍼.

handlers.py에서 추출한 기사 매칭/매핑 유틸리티.
check, report 파이프라인 모두에서 사용.
"""

import re
from datetime import datetime


# handlers.py의 _SKIP_TITLE_TAGS도 여기로 이동
_SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[움짤]", "[화보]", "[광고]", "[AD]"}


def normalize_title(title: str) -> str:
    """제목 정규화. HTML 태그 제거, 공백 정리."""
    # handlers.py의 _normalize_title 로직 그대로 복사
    ...


def match_article(llm_title: str, articles: list[dict]) -> dict | None:
    """LLM 출력 제목으로 원본 기사를 매칭한다.

    매칭 우선순위:
    1. 정확 일치
    2. 정규화 후 일치
    3. substring(15자+) 매칭
    """
    # handlers.py의 _match_article 로직 그대로 복사
    ...


def map_results_to_articles(
    results: list[dict], articles: list[dict], url_key: str = "url",
) -> None:
    """LLM 분석 결과에 원본 기사의 URL, publisher, pub_time을 주입한다.

    source_indices/merged_indices로 매칭, fallback으로 title 매칭.
    """
    # handlers.py의 _map_results_to_articles 로직 그대로 복사
    ...


def has_skip_tag(title: str) -> bool:
    """제목에 스킵 태그가 포함되어 있는지 확인한다."""
    return any(tag in title for tag in _SKIP_TITLE_TAGS)
```

**구현 방법**: handlers.py를 읽고, 위 함수들의 구현을 **그대로** 복사한다.
함수명에서 앞의 `_` (private prefix)를 제거하여 public으로 변경.

### 2. src/pipelines/check.py

```python
"""타사 체크 파이프라인.

handlers.py의 _run_check_pipeline을 독립 모듈로 분리.
메시지 전송 로직은 포함하지 않음 (순수 파이프라인).
"""

from datetime import datetime, timedelta, timezone

from src.config import CHECK_MAX_WINDOW_SECONDS
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.check_agent import filter_check_articles, analyze_articles
from src.storage.repository import (
    get_recent_reported_articles,
    save_reported_articles,
    update_last_check_at,
)
from src.pipelines import normalize_title, match_article, map_results_to_articles, has_skip_tag


async def run_check(
    db, journalist: dict,
) -> tuple[list[dict] | None, datetime, datetime, int]:
    """타사 체크 파이프라인을 실행한다. 메시지 전송 없음.

    Args:
        db: aiosqlite 연결
        journalist: get_journalist 반환값

    Returns:
        (results, since, now, haiku_filtered_count)
        results: 분석 결과 리스트 | None (기사 없음)
    """
    # handlers.py의 _run_check_pipeline 로직 그대로 복사
    # 핵심 흐름:
    # 1. since 계산 (last_check_at 또는 CHECK_MAX_WINDOW_SECONDS)
    # 2. search_news(keywords, since)
    # 3. filter_by_publisher
    # 4. 제목 태그 필터 (has_skip_tag)
    # 5. filter_check_articles (Haiku 필터)
    # 6. fetch_articles_batch (본문 스크래핑)
    # 7. analyze_articles (Haiku 분석)
    # 8. map_results_to_articles
    # 9. save_reported_articles + update_last_check_at
    # 10. return (results, since, now, haiku_filtered_count)
    ...
```

### 3. src/pipelines/report.py

```python
"""부서 브리핑 파이프라인.

handlers.py의 _run_report_pipeline을 독립 모듈로 분리.
메시지 전송 로직은 포함하지 않음 (순수 파이프라인).
"""

from datetime import datetime, timedelta, timezone

from src.config import REPORT_MAX_WINDOW_SECONDS, DEPARTMENT_PROFILES
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.report_agent import filter_articles, analyze_report_articles
from src.storage.repository import (
    get_recent_report_items,
    save_report_items,
    update_report_item,
    update_last_report_at,
    get_or_create_report_cache,
    get_report_items_by_cache,
)
from src.pipelines import normalize_title, match_article, map_results_to_articles


async def run_report(
    db, journalist: dict, existing_items: list[dict] | None = None,
) -> list[dict] | None:
    """브리핑 파이프라인을 실행한다. 메시지 전송 없음.

    Args:
        db: aiosqlite 연결
        journalist: get_journalist 반환값
        existing_items: 기존 브리핑 항목 (시나리오 B). None이면 시나리오 A.

    Returns:
        분석 결과 리스트 | None
    """
    # handlers.py의 _run_report_pipeline 로직 그대로 복사
    # 핵심 흐름:
    # 1. since 계산
    # 2. search_news(report_keywords, since)
    # 3. filter_by_publisher
    # 4. filter_articles (Haiku 필터)
    # 5. fetch_articles_batch (본문 스크래핑)
    # 6. analyze_report_articles
    # 7. map_results_to_articles
    # 8. return results
    ...
```

---

## 구현 절차

1. **handlers.py를 Read tool로 읽는다** (전체 내용)
2. 다음 함수들을 식별한다:
   - `_normalize_title`
   - `_match_article`
   - `_map_results_to_articles`
   - `_run_check_pipeline`
   - `_run_report_pipeline`
   - `_SKIP_TITLE_TAGS` (상수)
3. 공유 헬퍼를 `pipelines/__init__.py`에 복사 (private prefix `_` 제거)
4. check 파이프라인을 `pipelines/check.py`에 복사
5. report 파이프라인을 `pipelines/report.py`에 복사
6. import 경로를 `src.pipelines`로 조정
7. **handlers.py 내 원본 함수는 그대로 둔다** (Integration 에이전트가 삭제)

---

## import 변환 예시

handlers.py 원본:
```python
from src.tools.search import search_news
from src.agents.check_agent import filter_check_articles, analyze_articles
```

pipelines/check.py:
```python
from src.tools.search import search_news
from src.agents.check_agent import filter_check_articles, analyze_articles
from src.pipelines import map_results_to_articles, has_skip_tag  # __init__에서
```

---

## 테스트

`tests/test_pipelines.py`:

```python
import pytest
from src.pipelines import normalize_title, match_article, map_results_to_articles, has_skip_tag


def test_normalize_title():
    assert normalize_title("<b>제목</b>") == "제목"
    assert normalize_title("  공백  제목  ") == "공백 제목"


def test_match_article_exact():
    articles = [{"title": "삼성전자 실적 발표"}, {"title": "SK하이닉스 투자"}]
    result = match_article("삼성전자 실적 발표", articles)
    assert result is not None
    assert result["title"] == "삼성전자 실적 발표"


def test_match_article_no_match():
    articles = [{"title": "완전 다른 기사"}]
    result = match_article("존재하지 않는 제목", articles)
    assert result is None


def test_has_skip_tag():
    assert has_skip_tag("[포토] 연예인 사진") is True
    assert has_skip_tag("삼성전자 실적 발표") is False


def test_map_results_to_articles():
    results = [{"source_indices": [0], "title": "테스트 기사"}]
    articles = [{"title": "테스트 기사", "url": "https://example.com", "pubDate": "2026-01-01"}]
    map_results_to_articles(results, articles, url_key="url")
    assert results[0].get("url") == "https://example.com"
```

---

## 완료 기준

1. `pipelines/__init__.py`, `check.py`, `report.py` 3개 파일 생성
2. handlers.py의 파이프라인 로직이 1:1로 복사됨
3. `run_check()`, `run_report()`의 시그니처가 인터페이스 계약과 일치
4. 공유 헬퍼 함수가 public으로 노출됨
5. import 경로가 올바르게 설정됨
6. handlers.py 원본은 변경 없음
7. 테스트 통과
