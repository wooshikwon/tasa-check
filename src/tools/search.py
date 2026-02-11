"""네이버 뉴스 검색 API 모듈."""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from src.config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
_DISPLAY = 100
_MAX_PAGES = 2
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _build_query(keywords: list[str]) -> str:
    """키워드 리스트를 OR 조건 쿼리 문자열로 결합."""
    return " | ".join(keywords)


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


async def search_news(
    keywords: list[str],
    since: datetime,
) -> list[dict]:
    """키워드로 네이버 뉴스를 검색하여 since 이후 기사만 반환.

    최대 2페이지(200건)까지 조회하며, pubDate가 since보다 이전인
    기사가 나타나면 해당 페이지 이후 조회를 중단한다.

    Args:
        keywords: 검색 키워드 리스트. OR 조건으로 결합된다.
        since: 이 시각 이후에 발행된 기사만 포함.

    Returns:
        정제된 기사 dict 리스트. 각 dict는 title, link,
        originallink, description, pubDate 키를 포함한다.
    """
    query = _build_query(keywords)
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    results: list[dict] = []

    async with httpx.AsyncClient() as client:
        for page in range(_MAX_PAGES):
            start = page * _DISPLAY + 1
            params = {
                "query": query,
                "display": _DISPLAY,
                "start": start,
                "sort": "date",
            }

            resp = await client.get(
                _SEARCH_URL, headers=headers, params=params,
            )
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

            # 시간 범위 밖 기사가 등장하면 다음 페이지 조회 불필요
            if hit_old:
                break

            # 반환 건수가 display보다 적으면 마지막 페이지
            if len(items) < _DISPLAY:
                break

    return results
