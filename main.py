"""타사 체크 Telegram Bot 진입점."""

import logging

from telegram.ext import Application, CommandHandler

from src.config import TELEGRAM_BOT_TOKEN, DB_PATH
from src.storage.models import init_db
from src.storage.repository import cleanup_old_data
from src.bot.conversation import build_conversation_handler
from src.bot.handlers import check_handler, setkey_handler

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """앱 시작 시 DB 초기화 + 캐시 정리."""
    db = await init_db(DB_PATH)
    application.bot_data["db"] = db
    await cleanup_old_data(db)
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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /start 프로필 등록 (ConversationHandler)
    app.add_handler(build_conversation_handler())

    # /check 타사 체크
    app.add_handler(CommandHandler("check", check_handler))

    # /setkey API 키 변경
    app.add_handler(CommandHandler("setkey", setkey_handler))

    logger.info("봇 시작 (polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
