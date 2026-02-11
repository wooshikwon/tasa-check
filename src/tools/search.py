"""네이버 뉴스 검색 API 모듈."""

from __future__ import annotations

import asyncio
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
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """HTML 태그 제거."""
    return _HTML_TAG_RE.sub("", text)


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

        resp = await client.get(_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

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
) -> list[dict]:
    """키워드별로 네이버 뉴스를 개별 검색하여 since 이후 기사만 반환.

    네이버 API는 boolean OR 연산자를 공식 지원하지 않으므로,
    키워드별 개별 검색 후 URL 기준 중복 제거하여 병합한다.

    Args:
        keywords: 검색 키워드 리스트. 각 키워드별로 개별 검색한다.
        since: 이 시각 이후에 발행된 기사만 포함.

    Returns:
        정제된 기사 dict 리스트 (최신순 정렬, 최대 200건).
    """
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    async with httpx.AsyncClient() as client:
        tasks = [
            _search_keyword(client, kw, since, headers)
            for kw in keywords
        ]
        keyword_results_list = await asyncio.gather(*tasks)

    # URL 기준 중복 제거 후 병합
    seen_urls: set[str] = set()
    results: list[dict] = []
    for keyword_results in keyword_results_list:
        for article in keyword_results:
            url = article["originallink"]
            if url not in seen_urls:
                seen_urls.add(url)
                results.append(article)

    results.sort(key=lambda x: x["pubDate"], reverse=True)
    logger.info("키워드 %d개 검색 완료: %d건 수집", len(keywords), len(results))
    return results[:_MAX_TOTAL_RESULTS]
