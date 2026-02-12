"""타사 체크 Telegram Bot 진입점."""

import logging
import os

from dotenv import load_dotenv
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
from src.bot.handlers import check_handler, report_handler, setkey_handler, setdivision_handler, setdivision_callback
from src.bot.scheduler import schedule_handler, restore_schedules

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """앱 시작 시 DB 초기화 + 캐시 정리 + 스케줄 복원."""
    db = await init_db(DB_PATH)
    application.bot_data["db"] = db
    await cleanup_old_data(db)
    await restore_schedules(application, db)
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

    # /setkey API 키 변경
    app.add_handler(CommandHandler("setkey", setkey_handler))

    # /setdivision 부서 변경
    app.add_handler(CommandHandler("setdivision", setdivision_handler))
    app.add_handler(CallbackQueryHandler(setdivision_callback, pattern="^setdiv:"))

    # /schedule 자동 실행 예약
    app.add_handler(CommandHandler("schedule", schedule_handler))

    logger.info("봇 시작 (polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
