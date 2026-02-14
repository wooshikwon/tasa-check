"""네이버 뉴스 검색 API 모듈."""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from src.config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
_DISPLAY = 100
_MAX_PAGES = 2
_MAX_TOTAL_RESULTS = 200
_BATCH_SIZE = 3  # 동시 요청 배치 크기
_BATCH_DELAY = 0.5  # 배치 간 대기 (초)
_RETRY_MAX = 2
_RETRY_DELAY = 1.0  # 429 재시도 대기 (초)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """HTML 태그 제거 + HTML 엔티티 디코딩."""
    return html_module.unescape(_HTML_TAG_RE.sub("", text))


def _parse_pub_date(raw: str) -> datetime:
    """RFC 2822 형식의 pubDate 문자열을 datetime으로 변환."""
    return parsedate_to_datetime(raw)


def _parse_item(item: dict) -> dict:
    """API 응답의 개별 아이템을 정제된 dict로 변환."""
    return {
        "title": _strip_html(item["title"]),
        "link": item["link"],
        "originallink": item["originallink"],
        "description": _strip_html(item["description"]),
        "pubDate": _parse_pub_date(item["pubDate"]),
    }


async def _request_with_retry(
    client: httpx.AsyncClient,
    headers: dict,
    params: dict,
) -> dict | None:
    """네이버 API 요청. 429 시 재시도한다."""
    for attempt in range(_RETRY_MAX + 1):
        resp = await client.get(_SEARCH_URL, headers=headers, params=params)
        if resp.status_code == 429:
            if attempt < _RETRY_MAX:
                delay = _RETRY_DELAY * (attempt + 1)
                logger.warning("429 Rate Limited, %.1f초 후 재시도 (%d/%d)", delay, attempt + 1, _RETRY_MAX)
                await asyncio.sleep(delay)
                continue
            logger.error("429 재시도 한도 초과, 키워드 '%s' 건너뜀", params.get("query"))
            return None
        resp.raise_for_status()
        return resp.json()
    return None


async def _search_keyword(
    client: httpx.AsyncClient,
    keyword: str,
    since: datetime,
    headers: dict,
) -> list[dict]:
    """단일 키워드로 네이버 뉴스를 검색한다."""
    results: list[dict] = []

    for page in range(_MAX_PAGES):
        start = page * _DISPLAY + 1
        params = {
            "query": keyword,
            "display": _DISPLAY,
            "start": start,
            "sort": "date",
        }

        data = await _request_with_retry(client, headers, params)
        if data is None:
            break

        items = data.get("items", [])
        if not items:
            break

        hit_old = False
        for item in items:
            parsed = _parse_item(item)
            if parsed["pubDate"] >= since:
                results.append(parsed)
            else:
                hit_old = True

        if hit_old:
            break

        if len(items) < _DISPLAY:
            break

    return results


async def search_news(
    keywords: list[str],
    since: datetime,
    max_results: int = _MAX_TOTAL_RESULTS,
) -> list[dict]:
    """키워드별로 네이버 뉴스를 검색하여 since 이후 기사만 반환.

    네이버 API rate limit을 피하기 위해 _BATCH_SIZE개씩 나눠 요청하고,
    배치 사이에 _BATCH_DELAY초 대기한다. 429 발생 시 자동 재시도한다.

    Args:
        keywords: 검색 키워드 리스트. 각 키워드별로 개별 검색한다.
        since: 이 시각 이후에 발행된 기사만 포함.
        max_results: 반환할 최대 기사 수. 기본값 200, report는 400 전달.

    Returns:
        정제된 기사 dict 리스트 (최신순 정렬, 최대 max_results건).
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    all_results: list[list[dict]] = []
    async with httpx.AsyncClient() as client:
        for i in range(0, len(keywords), _BATCH_SIZE):
            batch = keywords[i:i + _BATCH_SIZE]
            tasks = [
                _search_keyword(client, kw, since, headers)
                for kw in batch
            ]
            batch_results = await asyncio.gather(*tasks)
            all_results.extend(batch_results)

            # 마지막 배치가 아니면 대기
            if i + _BATCH_SIZE < len(keywords):
                await asyncio.sleep(_BATCH_DELAY)

    # URL 기준 중복 제거 후 병합
    seen_urls: set[str] = set()
    results: list[dict] = []
    for keyword_results in all_results:
        for article in keyword_results:
            url = article["originallink"]
            if url not in seen_urls:
                seen_urls.add(url)
                results.append(article)

    results.sort(key=lambda x: x["pubDate"], reverse=True)
    logger.info("키워드 %d개 검색 완료: %d건 수집", len(keywords), len(results))
    return results[:max_results]
