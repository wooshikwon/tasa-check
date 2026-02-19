"""타사 체크 파이프라인.

handlers.py의 _run_check_pipeline을 독립 모듈로 분리.
메시지 전송, DB 저장/갱신 로직은 포함하지 않음 (순수 파이프라인).
"""

import logging
from datetime import UTC, datetime, timedelta

from src.config import CHECK_MAX_WINDOW_SECONDS
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.check_agent import filter_check_articles, analyze_articles
from src.storage.repository import get_recent_reported_articles
from src.pipelines import map_results_to_articles, has_skip_tag

logger = logging.getLogger(__name__)


async def run_check(
    db, journalist: dict,
) -> tuple[list[dict] | None, datetime, datetime, int]:
    """네이버 검색 -> 필터 -> 본문 수집 -> Claude 분석 파이프라인.

    Args:
        db: aiosqlite 연결
        journalist: get_journalist 반환값

    Returns:
        (분석 결과 리스트, since, now, haiku_filtered). 기사가 없으면 결과는 None.
    """
    now = datetime.now(UTC)
    last_check = journalist["last_check_at"]
    if last_check:
        last_dt = datetime.fromisoformat(last_check).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
    else:
        window_seconds = CHECK_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)

    # 네이버 뉴스 수집 (Haiku 필터가 노이즈를 걸러주므로 400건까지 확대)
    raw_articles = await search_news(journalist["keywords"], since, max_results=300)
    if not raw_articles:
        return None, since, now, 0

    # 언론사 필터링
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        return None, since, now, 0

    # 제목 기반 필터링 (분석 가치 없는 기사 제거)
    filtered = [
        a for a in filtered
        if not has_skip_tag(a.get("title", ""))
    ]
    if not filtered:
        return None, since, now, 0

    # Haiku 사전 필터 (부서 관련성)
    pre_filter_count = len(filtered)
    filtered = await filter_check_articles(
        journalist["api_key"], filtered,
        journalist["department"],
    )
    haiku_filtered = pre_filter_count - len(filtered)
    if not filtered:
        return None, since, now, haiku_filtered

    # 본문 수집 (Haiku 통과 기사만 스크래핑)
    urls = [a["link"] for a in filtered]
    bodies = await fetch_articles_batch(urls)

    # Claude 분석용 데이터 조립
    articles_for_analysis = []
    for a in filtered:
        publisher = get_publisher_name(a["originallink"]) or ""
        body = bodies.get(a["link"], "") or ""
        pub_date_str = a["pubDate"].strftime("%Y-%m-%d %H:%M") if hasattr(a["pubDate"], "strftime") else str(a["pubDate"])
        articles_for_analysis.append({
            "title": a["title"],
            "publisher": publisher,
            "body": body,
            "url": a["link"],
            "pubDate": pub_date_str,
        })

    # 이전 check 보고 이력 로드
    history = await get_recent_reported_articles(db, journalist["id"], hours=72)

    # Claude API 분석
    results = await analyze_articles(
        api_key=journalist["api_key"],
        articles=articles_for_analysis,
        history=history,
        department=journalist["department"],
        keywords=journalist["keywords"],
    )

    # Claude는 기사 번호(index)만 반환 -> 원본 데이터에서 URL, 언론사를 주입
    if results:
        map_results_to_articles(results, articles_for_analysis, url_key="url")

    return results, since, now, haiku_filtered
