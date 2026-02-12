"""/check, /report, /setkey 명령 핸들러."""

import logging
from datetime import UTC, datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from src.config import CHECK_MAX_WINDOW_SECONDS
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.check_agent import analyze_articles
from src.agents.report_agent import run_report_agent
from src.storage import repository as repo
from src.bot.formatters import (
    format_check_header, format_article_message, format_no_results,
    format_no_important, format_skipped_articles,
    format_report_header_a, format_report_header_b,
    format_report_item,
)

logger = logging.getLogger(__name__)


async def _run_check_pipeline(db, journalist: dict) -> tuple[list[dict] | None, datetime, datetime]:
    """네이버 검색 → 필터 → 본문 수집 → Claude 분석 파이프라인.

    Returns:
        (분석 결과 리스트, since, now). 기사가 없으면 결과는 None.
    """
    now = datetime.now(UTC)
    last_check = journalist["last_check_at"]
    if last_check:
        last_dt = datetime.fromisoformat(last_check).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
    else:
        window_seconds = CHECK_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)

    # 네이버 뉴스 수집
    raw_articles = await search_news(journalist["keywords"], since)
    if not raw_articles:
        return None, since, now

    # 언론사 필터링
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        return None, since, now

    # 제목 기반 필터링 (분석 가치 없는 기사 제거)
    _SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}
    filtered = [
        a for a in filtered
        if not any(tag in a.get("title", "") for tag in _SKIP_TITLE_TAGS)
    ]
    if not filtered:
        return None, since, now

    # 본문 수집 (첫 1~2문단)
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
    history = await repo.get_recent_reported_articles(db, journalist["id"], hours=24)

    # Claude API 분석
    results = await analyze_articles(
        api_key=journalist["api_key"],
        articles=articles_for_analysis,
        history=history,
        department=journalist["department"],
        keywords=journalist["keywords"],
    )

    # Claude는 기사 번호(index)만 반환 → 원본 데이터에서 URL, 언론사를 주입
    if results:
        n = len(articles_for_analysis)
        for r in results:
            sources = r.pop("source_indices", [])
            merged = r.pop("merged_indices", [])
            valid_sources = [i for i in sources if 1 <= i <= n]
            valid_merged = [i for i in merged if 1 <= i <= n]

            r["article_urls"] = [articles_for_analysis[i - 1]["url"] for i in valid_sources]
            r["merged_from"] = [articles_for_analysis[i - 1]["url"] for i in valid_merged]
            if valid_sources:
                r["publisher"] = articles_for_analysis[valid_sources[0] - 1]["publisher"]
            elif not r.get("publisher"):
                r["publisher"] = ""

    return results, since, now


async def check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check 명령 처리. 설계 문서 8단계 흐름."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    # [1] 프로필 로드
    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    await update.message.reply_text("타사 체크 진행 중...")

    try:
        results, since, now = await _run_check_pipeline(db, journalist)
    except Exception as e:
        logger.error("타사 체크 실패: %s", e, exc_info=True)
        await update.message.reply_text(f"타사 체크 중 오류가 발생했습니다: {e}")
        return

    if results is None:
        await update.message.reply_text(format_no_results())
        return

    # [8] 결과 저장 + 기사별 전송
    reported = [r for r in results if r["category"] != "skip"]
    skipped = [r for r in results if r["category"] == "skip"]

    # DB에 보고 이력 저장 + 윈도우 갱신 (보고 대상이 있을 때만)
    if reported:
        await repo.save_reported_articles(db, journalist["id"], reported)
        await repo.update_last_check_at(db, journalist["id"])

    if not reported:
        await update.message.reply_text(format_no_important())
        # 스킵 기사가 있으면 목록 전송
        if skipped:
            await update.message.reply_text(
                format_skipped_articles(skipped), parse_mode="HTML", disable_web_page_preview=True,
            )
        return

    # 헤더 전송
    total = len(results)
    important = len(reported)
    await update.message.reply_text(
        format_check_header(total, important, since, now), parse_mode="HTML",
    )

    # 주요 기사 개별 메시지 전송
    for article in reported:
        msg = format_article_message(article)
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

    # 스킵 기사 목록 전송
    if skipped:
        await update.message.reply_text(
            format_skipped_articles(skipped), parse_mode="HTML", disable_web_page_preview=True,
        )


async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report 명령 처리. 부서 뉴스 브리핑."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    await update.message.reply_text("브리핑 생성 중...")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    department = journalist["department"]

    # 캐시 확인
    cache_id, is_new = await repo.get_or_create_report_cache(db, journalist["id"], today)

    existing_items = []
    if not is_new:
        existing_items = await repo.get_report_items_by_cache(db, cache_id)

    # 캐시 행은 있지만 items가 비어있으면 시나리오 A로 취급
    is_scenario_a = is_new or len(existing_items) == 0

    # 최근 태그 로드
    recent_tags = await repo.get_recent_report_tags(db, journalist["id"], days=3)

    # 에이전트 실행
    try:
        results = await run_report_agent(
            api_key=journalist["api_key"],
            department=department,
            date=today,
            recent_tags=recent_tags,
            existing_items=existing_items if not is_scenario_a else None,
        )
    except Exception as e:
        logger.error("report_agent 실행 실패: %s", e, exc_info=True)
        await update.message.reply_text(f"브리핑 생성 중 오류가 발생했습니다: {e}")
        return

    if is_scenario_a:
        await _handle_report_scenario_a(
            update, db, cache_id, department, today, results,
        )
    else:
        await _handle_report_scenario_b(
            update, db, cache_id, department, today,
            existing_items, results,
        )


async def _handle_report_scenario_a(
    update, db, cache_id, department, today, results,
) -> None:
    """시나리오 A: 당일 첫 요청. 전체 브리핑 생성."""
    if results:
        await repo.save_report_items(db, cache_id, results)

    if not results:
        await update.message.reply_text("관련 뉴스를 찾지 못했습니다.")
        return

    # 후속 항목을 앞에 정렬
    sorted_results = sorted(results, key=lambda r: r.get("category") != "follow_up")

    await update.message.reply_text(
        format_report_header_a(department, today, len(sorted_results)),
        parse_mode="HTML",
    )
    for item in sorted_results:
        msg = format_report_item(item)
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def _handle_report_scenario_b(
    update, db, cache_id, department, today,
    existing_items, delta_results,
) -> None:
    """시나리오 B: 당일 재요청. 기존 항목 + 변경분을 합쳐 전체 브리핑 출력."""
    # action 필드 보정
    for r in delta_results:
        if "action" not in r:
            r["action"] = "added"

    # 기존 항목에 수정 반영
    modified_ids: set[int] = set()
    delta_by_item_id = {}
    for r in delta_results:
        if r.get("action") == "modified" and r.get("item_id"):
            delta_by_item_id[r["item_id"]] = r

    merged_items: list[dict] = []
    for existing in existing_items:
        mod = delta_by_item_id.get(existing["id"])
        if mod:
            # 수정된 항목: 새 요약으로 교체
            merged = {**existing, "summary": mod["summary"], "action": "modified"}
            if mod.get("tags"):
                merged["tags"] = mod["tags"]
            merged_items.append(merged)
            modified_ids.add(existing["id"])
        else:
            # 변경 없는 항목
            merged_items.append({**existing, "action": "unchanged"})

    # 추가 항목 병합
    added = [r for r in delta_results if r.get("action") == "added"]
    merged_items.extend(added)

    # DB 반영
    if added:
        await repo.save_report_items(db, cache_id, added)
    for item_id, mod in delta_by_item_id.items():
        await repo.update_report_item(db, item_id, mod["summary"])

    # 변경 건수 계산
    modified_count = len(modified_ids)
    added_count = len(added)

    # 헤더 전송
    await update.message.reply_text(
        format_report_header_b(department, today, len(merged_items), modified_count, added_count),
        parse_mode="HTML",
    )

    # 수정/추가 항목을 앞에, 기존(unchanged) 항목을 뒤에 정렬
    action_order = {"modified": 0, "added": 1, "unchanged": 2}
    sorted_items = sorted(merged_items, key=lambda r: action_order.get(r.get("action", ""), 2))
    for item in sorted_items:
        msg = format_report_item(item, scenario_b=True)
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def setkey_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setkey 명령 처리. API 키 변경."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    # 인자로 키가 전달된 경우
    args = context.args
    if args:
        new_key = args[0]
        if not new_key.startswith("sk-"):
            await update.message.reply_text("API 키 형식이 올바르지 않습니다.")
            return

        # 키가 포함된 메시지 삭제 시도
        try:
            await update.message.delete()
        except Exception:
            pass

        await repo.update_api_key(db, telegram_id, new_key)
        await update.effective_chat.send_message("API 키가 변경되었습니다.")
    else:
        await update.message.reply_text(
            "사용법: /setkey sk-ant-your-new-key\n"
            "(입력 후 메시지가 자동 삭제됩니다)"
        )
