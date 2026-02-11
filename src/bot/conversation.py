"""프로필 등록 ConversationHandler.

상태 머신: ENTRY → NAME → DEPARTMENT → KEYWORDS → API_KEY → DONE
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.config import DEPARTMENTS
from src.storage import repository as repo

logger = logging.getLogger(__name__)

NAME, DEPARTMENT, KEYWORDS, API_KEY = range(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 시작. 이름을 묻는다."""
    await update.message.reply_text("타사 체크 봇입니다. 이름을 알려주세요.")
    return NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """이름 수신 → 부서 선택 Inline Keyboard 표시."""
    context.user_data["name"] = update.message.text.strip()

    keyboard = [
        [InlineKeyboardButton(dept, callback_data=dept) for dept in DEPARTMENTS]
    ]
    await update.message.reply_text(
        "담당 부서를 선택해주세요.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DEPARTMENT


async def receive_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """부서 선택 수신 → 키워드 입력 요청."""
    query = update.callback_query
    await query.answer()
    context.user_data["department"] = query.data

    await query.edit_message_text(
        f"부서: {query.data}\n\n"
        "모니터링 키워드를 입력해주세요. (쉼표 구분)\n"
        "예: 서부지검, 서부지법, 영등포경찰서"
    )
    return KEYWORDS


async def receive_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """키워드 수신 → API 키 입력 요청."""
    raw = update.message.text.strip()
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    if not keywords:
        await update.message.reply_text("키워드를 1개 이상 입력해주세요. (쉼표 구분)")
        return KEYWORDS

    context.user_data["keywords"] = keywords
    await update.message.reply_text(
        "Anthropic API 키를 입력해주세요.\n"
        "(1:1 DM이므로 타인에게 노출되지 않습니다)"
    )
    return API_KEY


async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """API 키 수신 → DB 저장 → 완료 메시지."""
    api_key = update.message.text.strip()
    if not api_key.startswith("sk-"):
        await update.message.reply_text(
            "API 키 형식이 올바르지 않습니다. sk-로 시작하는 키를 입력해주세요."
        )
        return API_KEY

    # 입력된 API 키 메시지 삭제 시도 (보안)
    try:
        await update.message.delete()
    except Exception:
        pass

    ud = context.user_data
    db = context.bot_data["db"]
    await repo.upsert_journalist(
        db,
        telegram_id=str(update.effective_user.id),
        name=ud["name"],
        department=ud["department"],
        keywords=ud["keywords"],
        api_key=api_key,
    )

    keywords_str = ", ".join(ud["keywords"])
    await update.effective_chat.send_message(
        f"설정 완료!\n"
        f"이름: {ud['name']} | 부서: {ud['department']}\n"
        f"키워드: {keywords_str}\n"
        f"/check - 타사 체크 | /report - 부서 브리핑"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 취소."""
    await update.message.reply_text("등록이 취소되었습니다.")
    context.user_data.clear()
    return ConversationHandler.END


def build_conversation_handler() -> ConversationHandler:
    """프로필 등록 ConversationHandler를 구성한다."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            DEPARTMENT: [CallbackQueryHandler(receive_department)],
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keywords)],
            API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
