"""타사 체크 Telegram Bot 진입점."""

import logging
import os
from datetime import time, timedelta, timezone

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

# Langfuse 자동 계측 (Anthropic API 호출을 자동 트레이싱)
load_dotenv()
if os.environ.get("LANGFUSE_PUBLIC_KEY"):
    from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
    from langfuse import get_client

    AnthropicInstrumentor().instrument()
    get_client()

from src.config import TELEGRAM_BOT_TOKEN, DB_PATH
from src.storage.models import init_db
from src.storage.repository import cleanup_old_data
from src.bot.conversation import build_conversation_handler
from src.bot.handlers import (
    check_handler, report_handler,
    set_division_handler, set_division_callback,
    status_handler, stats_handler,
)
from src.bot.settings import build_settings_handler
from src.bot.scheduler import restore_schedules

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _daily_cleanup(context) -> None:
    """매일 새벽 자동 실행: 오래된 캐시 데이터 정리."""
    db = context.bot_data.get("db")
    if db:
        await cleanup_old_data(db)
        logger.info("일일 캐시 정리 완료")


async def post_init(application: Application) -> None:
    """앱 시작 시 DB 초기화 + 캐시 정리 + 스케줄 복원."""
    db = await init_db(DB_PATH)
    application.bot_data["db"] = db
    await cleanup_old_data(db)
    await restore_schedules(application, db)

    # 매일 04:00 KST 자동 캐시 정리
    _KST = timezone(timedelta(hours=9))
    application.job_queue.run_daily(
        _daily_cleanup, time=time(hour=4, minute=0, tzinfo=_KST), name="daily_cleanup",
    )
    # 봇 명령어 목록 등록
    await application.bot.set_my_commands([
        BotCommand("check", "키워드 기반 타사 체크"),
        BotCommand("report", "부서 주요 뉴스 브리핑"),
        BotCommand("schedule", "자동 실행 예약 설정"),
        BotCommand("status", "현재 설정 조회"),
        BotCommand("set_keyword", "모니터링 키워드 변경"),
        BotCommand("set_apikey", "Claude API 키 변경"),
        BotCommand("set_division", "부서 변경"),
    ])

    logger.info("DB 초기화 완료: %s", DB_PATH)


async def post_shutdown(application: Application) -> None:
    """앱 종료 시 DB 연결 닫기."""
    db = application.bot_data.get("db")
    if db:
        await db.close()
        logger.info("DB 연결 종료")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /start 프로필 등록 (ConversationHandler)
    app.add_handler(build_conversation_handler())

    # /check 타사 체크
    app.add_handler(CommandHandler("check", check_handler))

    # /report 부서 뉴스 브리핑
    app.add_handler(CommandHandler("report", report_handler))

    # /set_keyword, /set_apikey, /schedule (2단계 대화형)
    app.add_handler(build_settings_handler())

    # /set_division 부서 변경
    app.add_handler(CommandHandler("set_division", set_division_handler))
    app.add_handler(CallbackQueryHandler(set_division_callback, pattern="^setdiv:"))

    # /status 현재 설정 조회
    app.add_handler(CommandHandler("status", status_handler))

    # /stats 관리자 전용 통계
    app.add_handler(CommandHandler("stats", stats_handler))

    logger.info("봇 시작 (polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
