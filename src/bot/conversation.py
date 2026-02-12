"""프로필 등록 ConversationHandler.

상태 머신: ENTRY → DEPARTMENT → KEYWORDS → API_KEY → DONE
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
from src.bot.scheduler import unregister_jobs

logger = logging.getLogger(__name__)

DEPARTMENT, KEYWORDS, API_KEY = range(3)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """대화 시작. 부서 선택 Inline Keyboard 표시."""
    # 부서 버튼을 2열로 배치
    keyboard = [
        DEPARTMENTS[i:i+2] for i in range(0, len(DEPARTMENTS), 2)
    ]
    keyboard = [
        [InlineKeyboardButton(dept, callback_data=dept) for dept in row]
        for row in keyboard
    ]
    await update.message.reply_text(
        "타사 체크 봇입니다. 담당 부서를 선택해주세요.",
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
    journalist_id = await repo.upsert_journalist(
        db,
        telegram_id=str(update.effective_user.id),
        department=ud["department"],
        keywords=ud["keywords"],
        api_key=api_key,
    )
    # 기존 report/check/schedule 데이터 초기화
    await repo.clear_journalist_data(db, journalist_id)
    unregister_jobs(context.application, journalist_id)

    keywords_str = ", ".join(ud["keywords"])
    await update.effective_chat.send_message(
        f"설정 완료!\n"
        f"부서: {ud['department']}\n"
        f"키워드: {keywords_str}\n"
        f"\n"
        f"사용 가능한 명령어:\n"
        f"/check - 키워드 기반 타사 체크\n"
        f"/report - 부서 주요 뉴스 브리핑\n"
        f"/schedule - 자동 실행 예약\n"
        f"  (예: /schedule check 09:00 12:00)\n"
        f"/schedule off - 자동 실행 예약 일괄 삭제\n"
        f"/setkey - Claude API 키 변경\n"
        f"/setdivision - 부서 변경"
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
            DEPARTMENT: [CallbackQueryHandler(receive_department)],
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keywords)],
            API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
