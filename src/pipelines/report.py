"""부서 브리핑 파이프라인.

handlers.py의 _run_report_pipeline을 독립 모듈로 분리.
메시지 전송, DB 저장/갱신 로직은 포함하지 않음 (순수 파이프라인).
"""

import logging
from datetime import UTC, datetime, timedelta

from src.config import REPORT_MAX_WINDOW_SECONDS, DEPARTMENT_PROFILES
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.report_agent import filter_articles, analyze_report_articles
from src.storage.repository import get_recent_report_items
from src.pipelines import map_results_to_articles

logger = logging.getLogger(__name__)


async def run_report(
    db, journalist: dict, existing_items: list[dict] | None = None,
) -> list[dict] | None:
    """네이버 검색 -> 언론사 필터 -> LLM 필터 -> 본문 수집 -> Claude 분석 파이프라인.

    Args:
        db: aiosqlite 연결
        journalist: get_journalist 반환값
        existing_items: 기존 브리핑 항목 (시나리오 B). None이면 시나리오 A.

    Returns:
        브리핑 항목 리스트. 수집 기사가 없으면 None.
    """
    now = datetime.now(UTC)
    last_report = journalist.get("last_report_at")
    if last_report:
        last_dt = datetime.fromisoformat(last_report).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), REPORT_MAX_WINDOW_SECONDS)
    else:
        window_seconds = REPORT_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)
    department = journalist["department"]
    dept_label = department if department.endswith("부") else f"{department}부"

    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    report_keywords = profile.get("report_keywords", [])
    if not report_keywords:
        return None

    # 네이버 API 수집 (report는 400건 상한)
    raw_articles = await search_news(report_keywords, since, max_results=300)
    if not raw_articles:
        return None

    # 언론사 필터
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        return None

    # LLM 필터 (Haiku) -- 제목+description 기반
    filtered = await filter_articles(journalist["api_key"], filtered, department)
    if not filtered:
        return None

    # 본문 수집 (첫 3문단)
    urls = [a["link"] for a in filtered]
    bodies = await fetch_articles_batch(urls)

    # 분석용 데이터 조립
    articles_for_analysis = []
    for a in filtered:
        publisher = get_publisher_name(a["originallink"]) or ""
        body = bodies.get(a["link"], "") or ""
        pub_date_str = (
            a["pubDate"].strftime("%Y-%m-%d %H:%M")
            if hasattr(a["pubDate"], "strftime")
            else str(a["pubDate"])
        )
        articles_for_analysis.append({
            "title": a["title"],
            "publisher": publisher,
            "body": body,
            "originallink": a["originallink"],
            "link": a["link"],
            "pubDate": pub_date_str,
        })

    # 이전 report 이력 (2일치)
    report_history = await get_recent_report_items(db, journalist["id"])

    # Claude 분석
    results = await analyze_report_articles(
        api_key=journalist["api_key"],
        articles=articles_for_analysis,
        report_history=report_history,
        existing_items=existing_items,
        department=department,
    )

    # source_indices -> URL, 언론사, 배포시각 역매핑
    if results:
        map_results_to_articles(results, articles_for_analysis, url_key="link")

        # 순번->DB ID 변환 (시나리오 B modified)
        if existing_items:
            seq_to_db_id = {
                seq: item["id"]
                for seq, item in enumerate(existing_items, 1)
            }
            for r in results:
                if r.get("item_id") and seq_to_db_id:
                    r["item_id"] = seq_to_db_id.get(r["item_id"], r["item_id"])

    return results
