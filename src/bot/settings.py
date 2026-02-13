"""설정 변경 ConversationHandler.

/set_keyword, /set_apikey, /schedule 명령을 2단계(안내 → 입력)로 처리한다.
"""

import logging
import re

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.storage import repository as repo
from src.bot.scheduler import register_job, unregister_jobs

logger = logging.getLogger(__name__)

AWAIT_KEYWORD, AWAIT_APIKEY, AWAIT_SCHEDULE = range(3)

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_MAX_TIMES = {"check": 30, "report": 30}


# --- /set_keyword ---

async def set_keyword_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/set_keyword 진입. 현재 키워드 표시 + 입력 안내."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return ConversationHandler.END

    current = ", ".join(journalist["keywords"]) if journalist["keywords"] else "(없음)"
    await update.message.reply_text(
        f"현재 키워드: {current}\n\n"
        "변경할 키워드를 입력해주세요. (쉼표 구분)\n"
        "예: 서부지검, 서부지법, 영등포경찰서\n\n"
        "/cancel — 취소"
    )
    return AWAIT_KEYWORD


async def receive_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """키워드 수신 처리."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    raw = update.message.text.strip()
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    if not keywords:
        await update.message.reply_text("키워드를 1개 이상 입력해주세요. (쉼표 구분)")
        return AWAIT_KEYWORD

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return ConversationHandler.END

    await repo.update_keywords(db, telegram_id, keywords)
    await repo.clear_check_data(db, journalist["id"])

    keywords_str = ", ".join(keywords)
    await update.message.reply_text(
        f"키워드가 변경되었습니다: {keywords_str}\n"
        f"체크 이력이 초기화되었습니다."
    )
    return ConversationHandler.END


# --- /set_apikey ---

async def set_apikey_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/set_apikey 진입. 마스킹된 현재 키 표시 + 입력 안내."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return ConversationHandler.END

    api_key = journalist.get("api_key", "")
    masked = f"{api_key[:7]}****" if len(api_key) >= 7 else "(미설정)"

    await update.message.reply_text(
        f"현재 API Key: {masked}\n\n"
        "새 API 키를 입력해주세요.\n"
        "(입력 후 메시지가 자동 삭제됩니다)\n\n"
        "/cancel — 취소"
    )
    return AWAIT_APIKEY


async def receive_apikey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """API 키 수신 처리. 입력 메시지를 자동 삭제한다."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    api_key = update.message.text.strip()
    if not api_key.startswith("sk-"):
        await update.message.reply_text(
            "API 키 형식이 올바르지 않습니다. sk-로 시작하는 키를 입력해주세요."
        )
        return AWAIT_APIKEY

    try:
        await update.message.delete()
    except Exception:
        pass

    await repo.update_api_key(db, telegram_id, api_key)
    await update.effective_chat.send_message("API 키가 변경되었습니다.")
    return ConversationHandler.END


# --- /schedule ---

async def schedule_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/schedule 진입. 현재 스케줄 표시 + 입력 안내."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return ConversationHandler.END

    context.user_data["_settings_journalist_id"] = journalist["id"]

    schedules = await repo.get_schedules(db, journalist["id"])
    check_times = [s["time_kst"] for s in schedules if s["command"] == "check"]
    report_times = [s["time_kst"] for s in schedules if s["command"] == "report"]

    lines = [
        "현재 자동 실행 설정:",
        f"  check: {', '.join(check_times) if check_times else '(없음)'}",
        f"  report: {', '.join(report_times) if report_times else '(없음)'}",
        "",
        "변경하려면 아래 형식으로 입력해주세요.",
        "  check 09:00 12:00 15:00 (최대 30건)",
        "  report 08:30 12:30 14:30 (최대 30건)",
        "  off — 전체 해제",
        "",
        "/cancel — 취소",
    ]
    await update.message.reply_text("\n".join(lines))
    return AWAIT_SCHEDULE


async def receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """스케줄 입력 수신 처리."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist_id = context.user_data.get("_settings_journalist_id")
    if not journalist_id:
        journalist = await repo.get_journalist(db, telegram_id)
        if not journalist:
            await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
            return ConversationHandler.END
        journalist_id = journalist["id"]

    raw = update.message.text.strip()
    args = raw.split()

    if not args:
        await update.message.reply_text("입력 형식이 올바르지 않습니다.")
        return AWAIT_SCHEDULE

    # off — 전체 해제
    if args[0].lower() == "off":
        await repo.delete_all_schedules(db, journalist_id)
        unregister_jobs(context.application, journalist_id)
        await update.message.reply_text("모든 자동 실행이 해제되었습니다.")
        context.user_data.pop("_settings_journalist_id", None)
        return ConversationHandler.END

    command = args[0].lower()
    if command not in ("check", "report"):
        await update.message.reply_text(
            "check 또는 report로 시작해야 합니다.\n"
            "예: check 09:00 12:00"
        )
        return AWAIT_SCHEDULE

    times = args[1:]
    if not times:
        await update.message.reply_text(
            f"시각을 입력해주세요.\n예: {command} 09:00 12:00"
        )
        return AWAIT_SCHEDULE

    max_count = _MAX_TIMES[command]
    if len(times) > max_count:
        await update.message.reply_text(f"{command}은 최대 {max_count}개까지 설정 가능합니다.")
        return AWAIT_SCHEDULE

    valid_times = []
    for t in times:
        if not _TIME_RE.match(t):
            await update.message.reply_text(
                f"시각 형식이 올바르지 않습니다: {t}\nHH:MM 형식으로 입력해주세요."
            )
            return AWAIT_SCHEDULE
        h, m = map(int, t.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            await update.message.reply_text(f"유효하지 않은 시각입니다: {t}")
            return AWAIT_SCHEDULE
        valid_times.append(t)

    await repo.save_schedules(db, journalist_id, command, valid_times)
    unregister_jobs(context.application, journalist_id, command=command)
    for t in valid_times:
        register_job(context.application, command, journalist_id, telegram_id, t)

    times_str = ", ".join(valid_times)
    await update.message.reply_text(
        f"자동 {command} 설정 완료!\n"
        f"매일 {times_str}에 자동 실행됩니다."
    )
    context.user_data.pop("_settings_journalist_id", None)
    return ConversationHandler.END


# --- 공통 ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """설정 변경 취소."""
    await update.message.reply_text("설정 변경이 취소되었습니다.")
    context.user_data.pop("_settings_journalist_id", None)
    return ConversationHandler.END


def build_settings_handler() -> ConversationHandler:
    """설정 변경 ConversationHandler를 구성한다."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("set_keyword", set_keyword_entry),
            CommandHandler("set_apikey", set_apikey_entry),
            CommandHandler("set_schedule", schedule_entry),
        ],
        states={
            AWAIT_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keyword)],
            AWAIT_APIKEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_apikey)],
            AWAIT_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="settings",
    )
