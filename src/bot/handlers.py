"""/check, /setkey 명령 핸들러."""

import logging
from datetime import UTC, datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from src.config import CHECK_MAX_WINDOW_SECONDS
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.check_agent import analyze_articles
from src.storage import repository as repo
from src.bot.formatters import format_check_header, format_article_message, format_no_results

logger = logging.getLogger(__name__)


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

    # 시간 윈도우 계산
    now = datetime.now(UTC)
    last_check = journalist["last_check_at"]
    if last_check:
        last_dt = datetime.fromisoformat(last_check).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
    else:
        window_seconds = CHECK_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)

    # [2] 네이버 뉴스 수집
    raw_articles = await search_news(journalist["keywords"], since)
    if not raw_articles:
        await repo.update_last_check_at(db, journalist["id"])
        await update.message.reply_text(format_no_results())
        return

    # [3] 언론사 필터링
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        await repo.update_last_check_at(db, journalist["id"])
        await update.message.reply_text(format_no_results())
        return

    # [4] 본문 수집 (첫 1~2문단)
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
            "url": a["originallink"],
            "pubDate": pub_date_str,
        })

    # [5] 맥락 로드 (optional)
    report_context = await repo.get_today_report_items(db, journalist["id"])

    # [6] 보고 이력 로드
    history = await repo.get_recent_reported_articles(db, journalist["id"], hours=24)

    # [7] Claude API 분석
    try:
        results = await analyze_articles(
            api_key=journalist["api_key"],
            articles=articles_for_analysis,
            report_context=report_context,
            history=history,
            department=journalist["department"],
        )
    except Exception as e:
        logger.error("Claude API 호출 실패: %s", e, exc_info=True)
        await update.message.reply_text(f"분석 중 오류가 발생했습니다: {e}")
        return

    # [8] 결과 저장 + 기사별 전송
    reported = [r for r in results if r["category"] != "skip"]
    all_non_skip = [r for r in results if r["category"] != "skip"]

    # DB에 보고 이력 저장
    if all_non_skip:
        await repo.save_reported_articles(db, journalist["id"], all_non_skip)

    # last_check_at 갱신 (결과와 무관)
    await repo.update_last_check_at(db, journalist["id"])

    if not reported:
        await update.message.reply_text(format_no_results())
        return

    # 헤더 전송
    total = len(results)
    important = len(reported)
    await update.message.reply_text(format_check_header(total, important))

    # 기사별 개별 메시지 전송
    for article in reported:
        msg = format_article_message(article)
        await update.message.reply_text(msg)


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
