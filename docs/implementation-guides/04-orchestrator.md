# D: Orchestrator 구현 가이드

> 담당: Pre-Callback + LLM Router + Conversation/Edit Article 핸들러

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/agents/orchestrator.py` | 신규 생성 — 전체 오케스트레이션 로직 |

## 금지 사항

- handlers.py, main.py 수정 금지 (Integration 에이전트 담당)
- models.py, repository.py 수정 금지 (Storage 에이전트 담당)
- 기존 agents/ 파일 (check_agent.py, report_agent.py) 수정 금지

---

## 외부 인터페이스 의존성

```python
# Storage Layer (Agent A)
from src.storage.repository import get_journalist, get_recent_conversations, save_conversation

# Pipelines (Agent C)
from src.pipelines.check import run_check
from src.pipelines.report import run_report

# Writing Agent (Agent E)
from src.agents.writing_agent import run_writing_agent

# Bot Infra (Agent B)
from src.bot.middleware import tracked_reply
from src.bot.formatters import (
    format_check_header, format_article_message, format_skipped_articles,
    format_report_header_a, format_report_item,
    format_writing_result,
)

# 기존 코드
from src.storage.repository import (
    get_or_create_report_cache, get_report_items_by_cache,
    save_report_items, get_today_report_items,
)
```

**참고**: 병렬 에이전트가 아직 구현하지 않은 함수를 import한다. 인터페이스 계약에 맞춰 코딩하면 Integration 단계에서 정상 동작한다.

---

## 노출 인터페이스

```python
async def pre_callback(
    db, api_key: str, telegram_id: str, current_query: str,
) -> dict
# 반환: {"relevant_messages": list[dict], "attachment_metas": list[dict]}

async def orchestrator_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None
```

---

## 상세 구현

### 파일 구조

```python
"""Orchestration Agent.

자연어 메시지를 분석하여 적절한 도구/파이프라인으로 라우팅한다.
기존 /command 핸들러를 대체하지 않으며, 자연어 입력만 처리한다.
"""

import asyncio
import json

import anthropic
from telegram import Update
from telegram.ext import ContextTypes

# ... imports ...

_writing_semaphore = asyncio.Semaphore(2)  # writing은 메모리 집약적, 동시 2건 제한
```

### 1. Pre-Callback (LLM 기반 대화 필터)

LLM으로 관련 대화를 선별하여 context를 구성한다.

```python
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

    # 대화가 5건 이하면 LLM 호출 없이 전부 반환 (비용 절감)
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

    # LLM에 요약 목록 전달 → 관련 대화 번호만 출력
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
        # LLM 호출 실패 시 fallback: 최근 5건 + 첨부파일 대화
        return _fallback_filter(conversations)

    # 선별된 번호로 전체 대화 로드
    selected_indices = []
    for block in message.content:
        if block.type == "tool_use" and block.name == "select_conversations":
            selected_indices = block.input.get("selected_indices", [])

    relevant = [conversations[idx - 1] for idx in selected_indices if 1 <= idx <= len(conversations)]

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
```

### 2. Router (route_to_tool)

```python
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
                        "topic": {"type": "string", "description": "기사 주제 (writing/edit_article 시)"},
                        "word_count": {"type": "integer", "description": "요청 분량"},
                        "search_keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "검색 키워드 2~3개 (writing 시)",
                        },
                        "has_attachment": {"type": "boolean", "description": "첨부파일 참조 필요 여부"},
                        "style_hint": {"type": "string", "description": "스타일 힌트 (스트레이트/기획 등)"},
                    },
                    "description": "도구 실행에 필요한 파라미터",
                },
            },
            "required": ["tool", "reason"],
        },
    }
]

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


async def _route(api_key: str, query: str, context_data: dict) -> dict:
    """LLM Router. Single-shot + forced tool_use."""
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
    """대화 맥락을 Router LLM 입력용 문자열로 포맷."""
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
```

### 3. Conversation 핸들러 (단순 대화)

```python
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
```

### 4. Edit Article 핸들러 (소폭 수정)

```python
_EDIT_ARTICLE_TOOL = {
    "name": "edit_article",
    "description": "기존 기사를 사용자의 요청에 따라 수정한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "수정된 제목 (변경 없으면 원문 그대로)"},
            "body": {"type": "string", "description": "수정된 본문"},
            "changes_made": {"type": "string", "description": "변경 사항 요약 (1문장)"},
        },
        "required": ["headline", "body", "changes_made"],
    },
}


async def handle_edit_article(api_key: str, query: str, context_data: dict) -> dict | str:
    """이전 작성 기사를 소폭 수정한다."""
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
    """
    for msg in messages:  # 최신순 정렬 전제
        if msg["role"] == "assistant" and len(msg["content"]) > 100:
            # 기사는 보통 100자 이상이고, 줄바꿈 포함
            if "\n" in msg["content"]:
                return msg["content"]
    return None
```

### 5. 메인 핸들러 (orchestrator_handler)

```python
_UNSUPPORTED_MIMES = {"application/x-hwp", "application/haansofthwp"}


def _is_unsupported_mime(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return mime_type in _UNSUPPORTED_MIMES


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
        await message.reply_text(f"처리 중 오류가 발생했습니다: {e}")
        return

    tool = route_result.get("tool", "reject")
    params = route_result.get("extracted_params", {})
    send_fn = message.reply_text

    # 라우팅 디스패치
    try:
        if tool == "check":
            await _dispatch_check(db, journalist, send_fn)
        elif tool == "report":
            await _dispatch_report(db, journalist, send_fn)
        elif tool == "writing":
            await _dispatch_writing(api_key, journalist, context_data, params, send_fn, context)
        elif tool == "edit_article":
            await _dispatch_edit(api_key, query, context_data, send_fn)
        elif tool == "conversation":
            reply = await handle_conversation(api_key, query, context_data)
            await tracked_reply(send_fn, db, telegram_id, reply)
        elif tool in ("schedule", "set_division", "set_keyword"):
            command_map = {"schedule": "/set_schedule", "set_division": "/set_division", "set_keyword": "/set_keyword"}
            await send_fn(f'{command_map[tool]} 명령어를 사용해주세요.')
        elif tool == "reject":
            reason = route_result.get("reason", "서비스 범위 밖의 요청입니다.")
            await send_fn(reason)
    except Exception as e:
        await send_fn(f"처리 중 오류가 발생했습니다: {e}")
```

### 6. 디스패치 함수들

```python
async def _dispatch_check(db, journalist, send_fn):
    """check 파이프라인 디스패치."""
    results, since, now, haiku_count = await run_check(db, journalist)
    if not results:
        await send_fn(format_no_results())
        return
    important = [r for r in results if r.get("category") in ("exclusive", "important")]
    skipped = [r for r in results if r.get("category") == "skip"]
    await send_fn(format_check_header(len(results), len(important), since, now))
    for article in important:
        await send_fn(format_article_message(article))
    if skipped:
        for msg in format_skipped_articles(skipped, haiku_count):
            await send_fn(msg)


async def _dispatch_report(db, journalist, send_fn):
    """report 파이프라인 디스패치."""
    # 기존 report_handler와 동일한 시나리오 A/B 로직
    # ... (handlers.py의 report_handler 내부 로직 참고)
    results = await run_report(db, journalist)
    if results is None:
        await send_fn("관련 기사를 찾지 못했습니다.")
        return
    # 포맷팅 + 전송 (기존 report_handler 포맷 로직 참고)
    ...


async def _dispatch_writing(api_key, journalist, context_data, params, send_fn, bot_context):
    """writing 에이전트 디스패치."""
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
    # dict → 포맷팅
    headline = result.get("headline", "")
    body = result.get("body", "")
    changes = result.get("changes_made", "")
    await send_fn(f"<b>{headline}</b>\n\n{body}\n\n<i>({changes})</i>", parse_mode="HTML")
```

---

## 기존 코드 패턴 참고

- **forced tool_use**: `tool_choice={"type": "tool", "name": "..."}` — check_agent.py와 동일
- **anthropic 클라이언트**: `anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)` — BYOK 패턴
- **db 접근**: `context.bot_data["db"]` — handlers.py와 동일
- **에러 처리**: try/except + 사용자 친화적 메시지

---

## 테스트

`tests/test_orchestrator.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


async def test_route_check():
    """'타사 체크 해줘' → check 라우팅."""
    with patch("src.agents.orchestrator.anthropic") as mock_anthropic:
        # mock LLM 응답: route_to_tool(tool="check")
        mock_response = _mock_route_response("check", "타사 체크 요청")
        mock_anthropic.AsyncAnthropic.return_value.messages.create = AsyncMock(return_value=mock_response)

        from src.agents.orchestrator import _route
        result = await _route("test-key", "타사 체크 해줘", {"relevant_messages": []})
        assert result["tool"] == "check"


async def test_route_writing():
    """'기사 써줘' → writing 라우팅."""
    # 유사 패턴으로 writing 라우팅 테스트
    ...


async def test_pre_callback_few_conversations():
    """대화 5건 이하면 LLM 호출 없이 전부 반환."""
    with patch("src.agents.orchestrator.get_recent_conversations") as mock_get:
        mock_get.return_value = [
            {"id": 1, "role": "user", "content": "test", "attachment_meta": None, "message_type": "text", "created_at": "2026-02-19 10:00"},
        ]
        from src.agents.orchestrator import pre_callback
        result = await pre_callback(MagicMock(), "key", "123", "query")
        assert len(result["relevant_messages"]) == 1


async def test_file_only_message():
    """파일만 전송 시 안내 메시지 반환."""
    # orchestrator_handler에 document만 있고 text/caption 없는 경우 테스트
    ...


def _mock_route_response(tool, reason):
    """route_to_tool mock 응답 생성."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "route_to_tool"
    block.input = {"tool": tool, "reason": reason}
    response = MagicMock()
    response.content = [block]
    return response
```

---

## 완료 기준

1. `orchestrator.py` 신규 생성
2. `pre_callback`: 5건 이하 bypass, 6건 이상 LLM 필터, fallback 처리
3. `_route`: forced tool_use로 9개 tool 중 1개 선택
4. `handle_conversation`: 단일 LLM 호출 대화 응답
5. `handle_edit_article`: 단일 LLM 호출 기사 수정
6. `orchestrator_handler`: 파일 단독 처리, 텍스트 라우팅, 디스패치
7. 인터페이스 계약 시그니처 준수
8. 테스트 통과
