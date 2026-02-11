"""네이버 뉴스 검색 모듈 테스트."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.tools.search import (
    _strip_html,
    _parse_pub_date,
    search_news,
)

KST = timezone(timedelta(hours=9))


# --- HTML 태그 제거 ---

def test_strip_html_bold():
    """<b> 태그가 제거된다."""
    assert _strip_html("<b>서부지검</b> 수사") == "서부지검 수사"


def test_strip_html_no_tags():
    """태그가 없으면 원본 그대로."""
    assert _strip_html("일반 텍스트") == "일반 텍스트"


def test_strip_html_nested():
    """중첩 태그도 모두 제거."""
    assert _strip_html("<b><i>강조</i></b>") == "강조"


# --- pubDate 파싱 ---

def test_parse_pub_date():
    """RFC 2822 형식 문자열이 datetime으로 변환된다."""
    raw = "Wed, 11 Feb 2026 14:30:00 +0900"
    dt = _parse_pub_date(raw)
    assert dt.year == 2026
    assert dt.month == 2
    assert dt.day == 11
    assert dt.hour == 14
    assert dt.minute == 30
    assert dt.tzinfo is not None


# --- search_news ---

def _make_item(title: str, pub_date_str: str) -> dict:
    """테스트용 네이버 API 응답 아이템 생성."""
    return {
        "title": f"<b>{title}</b>",
        "link": f"https://n.news.naver.com/{title}",
        "originallink": f"https://example.com/{title}",
        "description": f"<b>{title}</b> 관련 기사입니다.",
        "pubDate": pub_date_str,
    }


def _make_response(items: list[dict], status_code: int = 200) -> httpx.Response:
    """httpx.Response mock 생성."""
    import json
    return httpx.Response(
        status_code=status_code,
        json={"lastBuildDate": "Wed, 11 Feb 2026 15:00:00 +0900", "total": len(items), "start": 1, "display": len(items), "items": items},
        request=httpx.Request("GET", _SEARCH_URL),
    )


_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
_SINCE = datetime(2026, 2, 11, 12, 0, 0, tzinfo=KST)


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_filters_old_articles():
    """since 이전 기사는 결과에서 제외된다."""
    items = [
        _make_item("최신기사", "Wed, 11 Feb 2026 14:00:00 +0900"),
        _make_item("오래된기사", "Wed, 11 Feb 2026 10:00:00 +0900"),
    ]
    mock_response = _make_response(items)

    async def mock_get(*args, **kwargs):
        return mock_response

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["서부지검"], _SINCE)

    assert len(results) == 1
    assert results[0]["title"] == "최신기사"


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_html_stripped():
    """결과의 title, description에서 HTML 태그가 제거된다."""
    items = [
        _make_item("테스트기사", "Wed, 11 Feb 2026 14:00:00 +0900"),
    ]
    mock_response = _make_response(items)

    async def mock_get(*args, **kwargs):
        return mock_response

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["테스트"], _SINCE)

    assert "<b>" not in results[0]["title"]
    assert "<b>" not in results[0]["description"]
    assert results[0]["title"] == "테스트기사"


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_pagination():
    """1페이지가 100건이면 2페이지를 추가 요청한다."""
    page1_items = [
        _make_item(f"기사{i}", "Wed, 11 Feb 2026 14:00:00 +0900")
        for i in range(100)
    ]
    page2_items = [
        _make_item(f"기사{i}", "Wed, 11 Feb 2026 13:00:00 +0900")
        for i in range(100, 130)
    ]

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(page1_items)
        return _make_response(page2_items)

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["서부지검"], _SINCE)

    assert call_count == 2
    assert len(results) == 130


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_no_second_page_when_fewer_than_display():
    """1페이지 결과가 100건 미만이면 2페이지를 요청하지 않는다."""
    items = [
        _make_item(f"기사{i}", "Wed, 11 Feb 2026 14:00:00 +0900")
        for i in range(50)
    ]

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_response(items)

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["서부지검"], _SINCE)

    assert call_count == 1
    assert len(results) == 50


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_stops_early_on_old_articles():
    """시간 범위 밖 기사가 나타나면 다음 페이지를 요청하지 않는다."""
    items = [
        _make_item(f"기사{i}", "Wed, 11 Feb 2026 14:00:00 +0900")
        for i in range(98)
    ]
    # 마지막 2건은 since 이전
    items.append(_make_item("구기사1", "Wed, 11 Feb 2026 10:00:00 +0900"))
    items.append(_make_item("구기사2", "Wed, 11 Feb 2026 09:00:00 +0900"))
    assert len(items) == 100

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_response(items)

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["서부지검"], _SINCE)

    # 100건 반환됐지만 old 기사가 있으므로 2페이지 요청하지 않음
    assert call_count == 1
    assert len(results) == 98


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_per_keyword_queries():
    """키워드별로 개별 API 호출이 실행된다."""
    items = [_make_item("기사", "Wed, 11 Feb 2026 14:00:00 +0900")]
    mock_response = _make_response(items)

    captured_queries = []

    async def mock_get(*args, **kwargs):
        params = kwargs.get("params", {})
        captured_queries.append(params.get("query"))
        return mock_response

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        await search_news(["서부지검", "서부지법"], _SINCE)

    assert "서부지검" in captured_queries
    assert "서부지법" in captured_queries
    assert len(captured_queries) == 2


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_deduplicates_across_keywords():
    """동일 URL 기사가 여러 키워드에서 수집되면 중복 제거된다."""
    # 두 키워드 검색에서 같은 originallink를 가진 기사가 반환됨
    items = [_make_item("중복기사", "Wed, 11 Feb 2026 14:00:00 +0900")]
    mock_response = _make_response(items)

    async def mock_get(*args, **kwargs):
        return mock_response

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["서부지검", "서부지법"], _SINCE)

    # 같은 기사가 두 번 검색됐지만 결과는 1건
    assert len(results) == 1


@pytest.mark.asyncio
@patch("src.tools.search.NAVER_CLIENT_ID", "test-id")
@patch("src.tools.search.NAVER_CLIENT_SECRET", "test-secret")
async def test_search_news_returns_correct_fields():
    """반환된 dict에 필수 키가 모두 포함된다."""
    items = [_make_item("테스트", "Wed, 11 Feb 2026 14:00:00 +0900")]
    mock_response = _make_response(items)

    async def mock_get(*args, **kwargs):
        return mock_response

    with patch("src.tools.search.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = mock_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        results = await search_news(["테스트"], _SINCE)

    item = results[0]
    assert set(item.keys()) == {"title", "link", "originallink", "description", "pubDate"}
    assert isinstance(item["pubDate"], datetime)
