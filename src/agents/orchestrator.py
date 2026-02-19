"""Orchestration Agent.

자연어 메시지를 분석하여 적절한 도구/파이프라인으로 라우팅한다.
기존 /command 핸들러를 대체하지 않으며, 자연어 입력만 처리한다.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import anthropic
from telegram import Update
from telegram.ext import ContextTypes

from src.storage.repository import (
    get_journalist, get_recent_conversations,
    update_last_check_at, save_reported_articles,
    get_or_create_report_cache, get_report_items_by_cache,
    update_last_report_at,
)
from src.pipelines.check import run_check
from src.pipelines.report import run_report
from src.agents.writing_agent import run_writing_agent
from src.bot.middleware import tracked_reply
from src.bot.handlers import (
    _user_locks, _pipeline_semaphore,
    _handle_report_scenario_a, _handle_report_scenario_b,
)
from src.bot.formatters import (
    format_check_header,
    format_article_message,
    format_skipped_articles,
    format_no_results,
    format_writing_result,
)

_KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)

# writing은 메모리 집약적이므로 동시 2건으로 제한
_writing_semaphore = asyncio.Semaphore(2)

# 지원하지 않는 MIME 타입
_UNSUPPORTED_MIMES = {"application/x-hwp", "application/haansofthwp"}


# ── Tool 정의 ─────────────────────────────────────────────────

_SELECT_CONVERSATIONS_TOOL = {
    "name": "select_conversations",
    "description": "현재 사용자 요청과 관련된 이전 대화 번호를 선별한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "관련 대화 번호 배열 (최근 3건은 항상 포함할 것)",
            },
        },
        "required": ["selected_indices"],
    },
}

_ROUTER_TOOLS = [
    {
        "name": "route_to_tool",
        "description": "사용자 요청을 적절한 도구로 라우팅한다",
        "input_schema": {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": [
                        "check", "report", "writing", "edit_article",
                        "conversation", "schedule", "set_division",
                        "set_keyword", "reject",
                    ],
                    "description": "실행할 도구",
                },
                "reason": {
                    "type": "string",
                    "description": "라우팅 판단 근거 (1문장)",
                },
                "extracted_params": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "기사 주제 (writing/edit_article 시)",
                        },
                        "word_count": {
                            "type": "integer",
                            "description": "요청 분량",
                        },
                        "search_keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "검색 키워드 2~3개 (writing 시)",
                        },
                        "has_attachment": {
                            "type": "boolean",
                            "description": "첨부파일 참조 필요 여부",
                        },
                        "style_hint": {
                            "type": "string",
                            "description": "스타일 힌트 (스트레이트/기획 등)",
                        },
                    },
                    "description": "도구 실행에 필요한 파라미터",
                },
            },
            "required": ["tool", "reason"],
        },
    }
]

_EDIT_ARTICLE_TOOL = {
    "name": "edit_article",
    "description": "기존 기사를 사용자의 요청에 따라 수정한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "수정된 제목 (변경 없으면 원문 그대로)",
            },
            "body": {
                "type": "string",
                "description": "수정된 본문",
            },
            "changes_made": {
                "type": "string",
                "description": "변경 사항 요약 (1문장)",
            },
        },
        "required": ["headline", "body", "changes_made"],
    },
}

# ── 시스템 프롬프트 ───────────────────────────────────────────

_ROUTER_SYSTEM_PROMPT = """당신은 기자용 뉴스 서비스의 라우터입니다.
사용자의 메시지와 대화 맥락을 분석하여 적절한 도구 하나를 선택합니다.

사용 가능한 도구:
- check: 키워드 기반 타사 체크 (타 언론사 단독/주요 기사 모니터링)
- report: 부서 뉴스 브리핑 (부서 주요 뉴스 요약)
- writing: 새로운 기사 작성 (보도자료, 키워드, 첨부파일 기반 기사 초안 생성)
- edit_article: 직전 작성 기사의 소폭 수정 (제목 변경, 분량 조절, 문단 수정 등)
- conversation: 서비스 안내, 감사 인사, 단순 질문 등 도구 실행이 불필요한 대화
- schedule: 자동 실행 예약 설정
- set_division: 부서 변경
- set_keyword: 키워드 변경
- reject: 서비스 범위 밖 요청 (사유 명시)

writing vs edit_article 판단:
- 대화 맥락에 직전 작성 기사가 있고, 사용자가 해당 기사의 부분 수정을 요청 → edit_article
- 새로운 주제/보도자료에 대한 기사 작성 요청 → writing
- 이전 기사와 전혀 다른 새 기사를 요청 → writing

conversation vs reject 판단:
- 서비스 관련 질문, 인사, 이전 결과에 대한 문의 → conversation
- 날씨, 주식 추천 등 서비스 범위 밖 → reject

writing 선택 시 extracted_params에 search_keywords, has_attachment, style_hint를 가능한 한 추출하라."""


# ── Pre-Callback ──────────────────────────────────────────────

async def pre_callback(
    db, api_key: str, telegram_id: str, current_query: str,
) -> dict:
    """LLM으로 관련 대화를 선별하고, DB에서 전체 내용을 로드한다.

    Returns:
        {
            "relevant_messages": [...],    # 선별된 대화 전체 내용
            "attachment_metas": [...],      # 선별된 대화 중 첨부파일 meta
        }
    """
    conversations = await get_recent_conversations(db, telegram_id, days=3, limit=50)
    if not conversations:
        return {"relevant_messages": [], "attachment_metas": []}

    # 대화가 5건 이하면 LLM 호출 없이 전부 반환
    if len(conversations) <= 5:
        attachment_metas = _extract_attachment_metas(conversations)
        return {"relevant_messages": conversations, "attachment_metas": attachment_metas}

    # 번호 붙은 요약 목록 생성 (LLM 입력용, 첫 80자만)
    summary_lines = []
    for i, c in enumerate(conversations, 1):
        truncated = c["content"][:80].replace("\n", " ")
        attach_tag = ""
        if c.get("attachment_meta"):
            meta = c["attachment_meta"]
            name = meta.get("file_name", "파일")
            size_mb = round(meta.get("file_size", 0) / 1_048_576, 1)
            attach_tag = f" [첨부: {name} {size_mb}MB]"
        date_str = c["created_at"][5:16]  # "MM-DD HH:MM"
        summary_lines.append(f"[{i}] {c['role']} {date_str} | \"{truncated}\"{attach_tag}")

    summary_text = "\n".join(summary_lines)

    # LLM에 요약 목록 전달하여 관련 대화 번호 선별
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            temperature=0.0,
            system=(
                "아래 대화 목록에서 현재 사용자 요청과 관련된 대화 번호를 선별하라.\n"
                "선별 기준:\n"
                "1. 최근 3건은 항상 포함 (직전 맥락)\n"
                "2. 현재 요청의 주제·키워드와 관련된 대화\n"
                "3. 첨부파일이 있는 대화 (현재 요청이 기사 작성이면 특히 중요)\n"
                "4. 관련 없는 대화는 제외하여 context 오염 방지\n"
                "select_conversations 도구로 번호만 제출하라."
            ),
            messages=[{"role": "user", "content": f"현재 요청: {current_query}\n\n{summary_text}"}],
            tools=[_SELECT_CONVERSATIONS_TOOL],
            tool_choice={"type": "tool", "name": "select_conversations"},
        )
    except Exception:
        logger.warning("pre_callback LLM 호출 실패, fallback 적용", exc_info=True)
        return _fallback_filter(conversations)

    # 선별된 번호로 대화 로드
    selected_indices = []
    for block in message.content:
        if block.type == "tool_use" and block.name == "select_conversations":
            selected_indices = block.input.get("selected_indices", [])

    relevant = [
        conversations[idx - 1]
        for idx in selected_indices
        if 1 <= idx <= len(conversations)
    ]

    # 선별 결과가 비어있으면 최근 3건으로 fallback
    if not relevant:
        relevant = conversations[:3]

    attachment_metas = _extract_attachment_metas(relevant)
    return {"relevant_messages": relevant, "attachment_metas": attachment_metas}


def _extract_attachment_metas(conversations: list[dict]) -> list[dict]:
    """대화 목록에서 첨부파일 meta를 추출한다."""
    return [
        {**c["attachment_meta"], "message_id": c["id"], "created_at": c["created_at"]}
        for c in conversations
        if c.get("attachment_meta")
    ]


def _fallback_filter(conversations: list[dict]) -> dict:
    """LLM 실패 시 규칙 기반 fallback. 최근 5건 + 첨부파일 대화."""
    recent = conversations[:5]
    attach_convos = [c for c in conversations[5:] if c.get("attachment_meta")]
    relevant = recent + attach_convos
    return {
        "relevant_messages": relevant,
        "attachment_metas": _extract_attachment_metas(relevant),
    }


# ── Router ────────────────────────────────────────────────────

async def _route(api_key: str, query: str, context_data: dict) -> dict:
    """LLM Router. Single-shot + forced tool_use로 도구 1개를 선택한다."""
    context_text = _format_context_for_router(context_data)
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)

    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        temperature=0.0,
        system=_ROUTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"{context_text}\n\n현재 요청: {query}"}],
        tools=_ROUTER_TOOLS,
        tool_choice={"type": "tool", "name": "route_to_tool"},
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == "route_to_tool":
            return block.input
    return {"tool": "reject", "reason": "라우팅 실패"}


def _format_context_for_router(context_data: dict) -> str:
    """대화 맥락을 Router LLM 입력용 문자열로 포맷한다."""
    messages = context_data.get("relevant_messages", [])
    if not messages:
        return ""
    lines = ["[대화 맥락]"]
    for m in messages[-10:]:  # 최근 10건만
        role = m["role"]
        content = m["content"][:200]
        attach = " [첨부파일]" if m.get("attachment_meta") else ""
        lines.append(f"{role}: {content}{attach}")
    return "\n".join(lines)


# ── Conversation 핸들러 ───────────────────────────────────────

async def handle_conversation(api_key: str, query: str, context_data: dict) -> str:
    """서비스 범위 내 단순 대화에 응답한다. 도구 실행 불필요."""
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
    context_text = _format_context_for_router(context_data)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        temperature=0.3,
        system="당신은 기자용 뉴스 서비스 보조입니다. 간결하게 답하세요.",
        messages=[{"role": "user", "content": f"{context_text}\n\n{query}"}],
    )
    return response.content[0].text


# ── Edit Article 핸들러 ───────────────────────────────────────

async def handle_edit_article(api_key: str, query: str, context_data: dict) -> dict | str:
    """이전 작성 기사를 소폭 수정한다. 에이전트 루프 없이 단일 LLM 호출."""
    previous_article = _extract_previous_article(context_data["relevant_messages"])
    if not previous_article:
        return "수정할 기사를 찾을 수 없습니다. 먼저 기사를 작성해주세요."

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        temperature=0.0,
        system="사용자가 요청한 부분만 최소한으로 수정하라. 요청하지 않은 부분은 원문 그대로 유지한다.",
        messages=[{"role": "user", "content": f"원문 기사:\n{previous_article}\n\n수정 요청: {query}"}],
        tools=[_EDIT_ARTICLE_TOOL],
        tool_choice={"type": "tool", "name": "edit_article"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "edit_article":
            return block.input
    return "수정에 실패했습니다. 다시 시도해주세요."


def _extract_previous_article(messages: list[dict]) -> str | None:
    """대화 맥락에서 직전 작성 기사를 찾는다.

    assistant 메시지 중 기사 형태(제목 + 본문)를 가진 마지막 메시지를 반환.
    최신순 정렬 전제.
    """
    for msg in messages:
        if msg["role"] == "assistant" and len(msg["content"]) > 100:
            if "\n" in msg["content"]:
                return msg["content"]
    return None


# ── 디스패치 함수 ─────────────────────────────────────────────

async def _dispatch_check(db, journalist, send_fn):
    """check 파이프라인 디스패치. handlers.py check_handler와 동일한 DB 영속화 수행."""
    async with _pipeline_semaphore:
        results, since, now, haiku_count = await run_check(db, journalist)

    # 파이프라인 완료 시 항상 last_check_at 갱신
    await update_last_check_at(db, journalist["id"])

    if not results:
        await send_fn(format_no_results())
        return

    # DB에 보고 기사 저장 (중복 보고 방지)
    await save_reported_articles(db, journalist["id"], results)

    reported = [r for r in results if r["category"] != "skip"]
    skipped = [r for r in results if r["category"] == "skip"]

    total = len(results) + haiku_count
    await send_fn(
        format_check_header(total, len(reported), since, now),
        parse_mode="HTML",
    )

    sorted_reported = sorted(reported, key=lambda r: r.get("pub_time", ""), reverse=True)
    for article in sorted_reported:
        await send_fn(format_article_message(article), parse_mode="HTML", disable_web_page_preview=True)

    if skipped:
        for msg in format_skipped_articles(skipped, haiku_count):
            await send_fn(msg, parse_mode="HTML", disable_web_page_preview=True)


async def _dispatch_report(db, journalist, send_fn):
    """report 파이프라인 디스패치. handlers.py report_handler와 동일한 시나리오 A/B 분기."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    department = journalist["department"]

    cache_id, is_new = await get_or_create_report_cache(db, journalist["id"], today)

    existing_items = []
    if not is_new:
        existing_items = await get_report_items_by_cache(db, cache_id)

    is_scenario_a = is_new or len(existing_items) == 0

    async with _pipeline_semaphore:
        results = await run_report(
            db, journalist,
            existing_items=existing_items if not is_scenario_a else None,
        )

    # 파이프라인 완료 시 항상 last_report_at 갱신
    await update_last_report_at(db, journalist["id"])

    if results is None:
        await send_fn("관련 기사를 찾지 못했습니다.")
        return

    if is_scenario_a:
        await _handle_report_scenario_a(
            send_fn, db, cache_id, department, today, results,
        )
    else:
        await _handle_report_scenario_b(
            send_fn, db, cache_id, department, today, existing_items, results,
        )


async def _dispatch_writing(api_key, journalist, context_data, params, send_fn, bot_context):
    """writing 에이전트 디스패치."""
    context_data["extracted_params"] = params
    context_data["journalist"] = journalist
    async with _writing_semaphore:
        await send_fn("기사를 작성하고 있습니다...")
        article = await run_writing_agent(api_key, context_data, bot_context)
        messages = format_writing_result(article)
        for msg in messages:
            await send_fn(msg, parse_mode="HTML")


async def _dispatch_edit(api_key, query, context_data, send_fn):
    """edit_article 핸들러 디스패치."""
    result = await handle_edit_article(api_key, query, context_data)
    if isinstance(result, str):
        await send_fn(result)
        return
    headline = result.get("headline", "")
    body = result.get("body", "")
    changes = result.get("changes_made", "")
    await send_fn(f"<b>{headline}</b>\n\n{body}\n\n<i>({changes})</i>", parse_mode="HTML")


# ── 유틸리티 ──────────────────────────────────────────────────

def _is_unsupported_mime(mime_type: str | None) -> bool:
    """지원하지 않는 MIME 타입인지 확인한다."""
    if not mime_type:
        return False
    return mime_type in _UNSUPPORTED_MIMES


# ── 메인 핸들러 ──────────────────────────────────────────────

async def orchestrator_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """자연어 메시지의 메인 진입점.

    MessageHandler(filters.TEXT & ~filters.COMMAND, group=1) 또는
    MessageHandler(filters.Document.ALL | filters.PHOTO, group=1)로 등록.
    """
    message = update.effective_message
    if not message or not update.effective_user:
        return

    query = message.text or message.caption or ""
    telegram_id = str(update.effective_user.id)
    db = context.bot_data["db"]

    # 사용자 조회
    journalist = await get_journalist(db, telegram_id)
    if not journalist:
        await message.reply_text("먼저 /start로 등록해주세요.")
        return

    api_key = journalist["api_key"]
    if not api_key:
        await message.reply_text("API 키가 설정되지 않았습니다. /set_apikey로 설정해주세요.")
        return

    # 파일 단독 전송 (캡션 없음)
    if not query and (message.document or message.photo):
        doc = message.document
        if doc and doc.file_size and doc.file_size > 3 * 1024 * 1024:
            await message.reply_text("파일 용량이 3MB를 초과합니다.")
            return
        if doc and _is_unsupported_mime(doc.mime_type):
            await message.reply_text("지원하지 않는 파일 형식입니다. (PDF, DOCX, TXT만 지원)")
            return
        await message.reply_text(
            "파일을 받았습니다. 어떻게 처리할까요?\n"
            '예) "이 보도자료로 300자 기사 써줘"'
        )
        return

    if not query:
        return

    # Pre-callback → Router → Dispatch
    try:
        context_data = await pre_callback(db, api_key, telegram_id, query)
        route_result = await _route(api_key, query, context_data)
    except Exception as e:
        logger.error("orchestrator 라우팅 실패: %s", e, exc_info=True)
        await message.reply_text(f"처리 중 오류가 발생했습니다: {e}")
        return

    tool = route_result.get("tool", "reject")
    params = route_result.get("extracted_params", {})
    send_fn = message.reply_text

    # check/report는 사용자별 동시 실행 방지 + 전역 세마포어 적용
    if tool in ("check", "report"):
        lock = _user_locks.setdefault(telegram_id, asyncio.Lock())
        if lock.locked():
            await send_fn("이전 요청이 처리 중입니다. 완료 후 다시 시도해주세요.")
            return
        try:
            async with lock:
                if tool == "check":
                    await send_fn("타사 체크 진행 중...")
                    await _dispatch_check(db, journalist, send_fn)
                else:
                    dept = journalist["department"]
                    dept_label = dept if dept.endswith("부") else f"{dept}부"
                    await send_fn(f"오늘 {dept_label} 브리핑 생성 중...")
                    await _dispatch_report(db, journalist, send_fn)
        except Exception as e:
            logger.error("orchestrator 디스패치 실패 (tool=%s): %s", tool, e, exc_info=True)
            await send_fn(f"처리 중 오류가 발생했습니다: {e}")
        return

    # 나머지 라우팅 디스패치
    try:
        if tool == "writing":
            await _dispatch_writing(api_key, journalist, context_data, params, send_fn, context)
        elif tool == "edit_article":
            await _dispatch_edit(api_key, query, context_data, send_fn)
        elif tool == "conversation":
            reply = await handle_conversation(api_key, query, context_data)
            await tracked_reply(send_fn, db, telegram_id, reply)
        elif tool in ("schedule", "set_division", "set_keyword"):
            command_map = {
                "schedule": "/set_schedule",
                "set_division": "/set_division",
                "set_keyword": "/set_keyword",
            }
            await send_fn(f'{command_map[tool]} 명령어를 사용해주세요.')
        elif tool == "reject":
            reason = route_result.get("reason", "서비스 범위 밖의 요청입니다.")
            await send_fn(reason)
    except Exception as e:
        logger.error("orchestrator 디스패치 실패 (tool=%s): %s", tool, e, exc_info=True)
        await send_fn(f"처리 중 오류가 발생했습니다: {e}")
