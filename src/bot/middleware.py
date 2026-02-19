"""Conversation Logger 미들웨어.

모든 수신 메시지와 봇 응답을 conversations 테이블에 저장한다.
Application.add_handler(MessageHandler(..., conversation_logger), group=-1) 으로 등록.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.storage.repository import save_conversation

logger = logging.getLogger(__name__)


async def conversation_logger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """모든 메시지를 conversations 테이블에 기록한다.

    group=-1로 등록하여 모든 핸들러보다 먼저 실행된다.
    저장 후 다음 핸들러로 전달 (return 없음 = 다음 group으로 전파).
    """
    message = update.effective_message
    if not message or not update.effective_user:
        return  # 콜백쿼리, 인라인쿼리 등은 스킵

    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    # 첨부파일 meta 추출
    attachment_meta = None
    if message.document:
        attachment_meta = {
            "file_id": message.document.file_id,
            "file_name": message.document.file_name,
            "mime_type": message.document.mime_type,
            "file_size": message.document.file_size,
        }
    elif message.photo:
        photo = message.photo[-1]  # 가장 큰 해상도
        attachment_meta = {
            "file_id": photo.file_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": photo.file_size,
        }

    content = message.text or message.caption or ""
    message_type = _classify_message_type(message)

    try:
        await save_conversation(db, telegram_id, "user", content, attachment_meta, message_type)
    except Exception:
        logger.warning("대화 로깅 실패", exc_info=True)


async def tracked_reply(original_reply_fn, db, telegram_id: str, text: str, **kwargs):
    """reply_text를 감싸서 봇 응답도 conversations에 저장한다.

    사용 예:
        result = await tracked_reply(
            update.message.reply_text, db, telegram_id, "응답 텍스트"
        )
    """
    result = await original_reply_fn(text, **kwargs)
    await save_conversation(db, telegram_id, "assistant", text, None, "text")
    return result


def _classify_message_type(message) -> str:
    """메시지 타입을 분류한다."""
    if message.document:
        return "document"
    if message.photo:
        return "photo"
    text = message.text or ""
    if text.startswith("/"):
        return "command"
    return "text"
