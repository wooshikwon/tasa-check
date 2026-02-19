# B: Bot Infra 구현 가이드

> 담당: Conversation Logger 미들웨어 + Writing 스타일 설정 + 기사 출력 포맷터

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/bot/middleware.py` | 신규 생성 — Conversation Logger, tracked_reply |
| `src/config.py` | 수정 — WRITING_STYLES 딕셔너리 추가 |
| `src/bot/formatters.py` | 수정 — Writing 결과 포맷 함수 추가 |

## 금지 사항

- handlers.py, main.py 수정 금지 (Integration 에이전트 담당)
- models.py, repository.py 수정 금지 (Storage 에이전트 담당)
- 기존 formatters.py 함수 변경 금지 (추가만)

---

## 외부 인터페이스 의존성

이 에이전트가 사용하는 다른 에이전트의 함수 (인터페이스 계약):
```python
# Storage Layer (Agent A)가 구현
from src.storage.repository import save_conversation, get_journalist
```

---

## 상세 구현

### 1. src/bot/middleware.py (신규 생성)

```python
"""Conversation Logger 미들웨어.

모든 수신 메시지와 봇 응답을 conversations 테이블에 저장한다.
Application.add_handler(MessageHandler(..., conversation_logger), group=-1) 으로 등록.
"""

from telegram import Update
from telegram.ext import ContextTypes

from src.storage.repository import save_conversation


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

    await save_conversation(db, telegram_id, "user", content, attachment_meta, message_type)


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
```

**핵심 동작**:
- `conversation_logger`는 `group=-1`로 등록 → 모든 핸들러보다 먼저 실행
- return 없이 종료 → python-telegram-bot이 다음 group의 핸들러로 전달
- `tracked_reply`는 기존 reply_text를 감싸는 래퍼. 새로운 Orchestrator/Writing 응답에서 사용
- 기존 check_handler, report_handler 등은 변경 없이 동작 (미들웨어가 입력을 기록하고, 출력은 별도 호출)

**`bot_data["db"]` 접근**: 기존 코드에서 `context.bot_data["db"]` 패턴을 이미 사용하고 있음 (main.py의 post_init에서 설정).

### 2. src/config.py — WRITING_STYLES 추가

기존 `DEPARTMENT_PROFILES` 다음에 추가:

```python
# 부서별 기사 작성 스타일 기본 가이드
WRITING_STYLES: dict[str, dict] = {
    "사회부": {
        "lead": "육하원칙 스트레이트. 첫 문장에 '누가 N일 무엇을 했다' 포함",
        "structure": "리드 → 핵심 팩트 → 배경 → 반응·전망",
        "tone": "객관적·건조체. '~했다' 종결",
        "forbidden": ["~것으로 알려졌다", "~관측이 나온다", "충격", "경악"],
        "length_default": "300~600자",
    },
    "정치부": {
        "lead": "육하원칙 스트레이트. 인물·기관 중심 서술",
        "structure": "리드 → 발언/결정 → 배경 → 파장·전망",
        "tone": "객관적·건조체. '~했다' 종결",
        "forbidden": ["~것으로 알려졌다", "파문", "충격"],
        "length_default": "300~600자",
    },
    "경제부": {
        "lead": "핵심 수치/변동 먼저 제시. '지난해 ~이 N% 증가해 N조원을 기록했다'",
        "structure": "리드(수치) → 세부 수치 → 원인 → 전망",
        "tone": "객관적·분석적. 수치와 비교 중심",
        "forbidden": ["~것으로 알려졌다", "폭등", "폭락", "대박"],
        "length_default": "300~600자",
    },
    "산업부": {
        "lead": "기업·제품·기술 중심 서술. 핵심 팩트 먼저",
        "structure": "리드 → 제품/기술 상세 → 시장 맥락 → 경쟁/전망",
        "tone": "객관적·기술적. 업계 용어 적절히 사용",
        "forbidden": ["~것으로 알려졌다", "혁신적인", "획기적인"],
        "length_default": "300~600자",
    },
    "테크부": {
        "lead": "기술·제품 핵심 변화 먼저. 독자 관점 서술",
        "structure": "리드 → 기술 상세 → 시장 영향 → 전망",
        "tone": "객관적이되 독자 친화적. 기술 용어 설명 병행",
        "forbidden": ["~것으로 알려졌다", "혁신적인", "놀라운"],
        "length_default": "300~600자",
    },
    "국제부": {
        "lead": "국가·기관 중심. 날짜(현지시각) 명시",
        "structure": "리드 → 결정/사건 상세 → 배경 → 국내 영향·전망",
        "tone": "객관적·분석적. '~일(현지 시각)' 표현",
        "forbidden": ["~것으로 알려졌다", "충격", "경악"],
        "length_default": "300~600자",
    },
    "문화부": {
        "lead": "작품·인물·행사 중심 서술",
        "structure": "리드 → 상세 내용 → 맥락 → 반응·의미",
        "tone": "객관적이되 부드러운 서술체",
        "forbidden": ["~것으로 알려졌다", "화제", "난리"],
        "length_default": "300~600자",
    },
    "스포츠부": {
        "lead": "경기 결과·기록 먼저. 스코어/순위 포함",
        "structure": "리드(결과) → 경기 하이라이트 → 선수 반응 → 향후 일정",
        "tone": "객관적이되 생동감 있는 서술",
        "forbidden": ["~것으로 알려졌다"],
        "length_default": "300~600자",
    },
}

WRITING_STYLES_DEFAULT: dict = {
    "lead": "육하원칙 스트레이트",
    "structure": "리드 → 핵심 팩트 → 배경 → 전망",
    "tone": "객관적·건조체",
    "forbidden": ["~것으로 알려졌다"],
    "length_default": "300~600자",
}
```

### 3. src/bot/formatters.py — Writing 출력 포맷 추가

기존 함수들 아래에 추가:

```python
def format_writing_result(article: dict) -> list[str]:
    """Writing Agent 결과를 Telegram 메시지로 포맷한다.

    article 필드:
        headline: str, body: str, word_count: int,
        sources: list[{title, url}], verified: str,
        verification_issues: list[dict] (optional)

    반환: 4096자 이하 메시지 리스트 (길면 분할)
    """
    headline = article.get("headline", "")
    body = article.get("body", "")
    word_count = article.get("word_count", len(body))
    sources = article.get("sources", [])
    verified = article.get("verified", "skipped")

    # 본문 구성
    parts = [f"<b>{_escape_html(headline)}</b>\n\n{_escape_html(body)}"]

    # 글자 수 + 검증 상태
    status_parts = [f"{word_count}자"]
    if verified == "revised":
        status_parts.append("팩트체크 수정됨")
    elif verified == "pass":
        status_parts.append("팩트체크 통과")
    parts.append(f"\n\n<i>({' | '.join(status_parts)})</i>")

    # 참고 기사 목록
    if sources:
        source_lines = ["\n\n──────────\n참고한 기사:"]
        for s in sources:
            title = _escape_html(s.get("title", ""))
            url = s.get("url", "")
            source_lines.append(f"- {title}\n  {url}")
        parts.append("\n".join(source_lines))

    full_text = "".join(parts)

    # 4096자 초과 시 분할
    if len(full_text) <= _MAX_MSG_LEN:
        return [full_text]
    return _split_writing_message(headline, body, sources, status_parts)


def _split_writing_message(headline, body, sources, status_parts):
    """긴 기사를 여러 메시지로 분할한다."""
    messages = []
    # 1/N: 제목 + 본문 (4000자 기준으로 자르기)
    header = f"<b>{_escape_html(headline)}</b>\n\n"
    max_body = _MAX_MSG_LEN - len(header) - 50
    body_escaped = _escape_html(body)

    if len(body_escaped) <= max_body:
        messages.append(header + body_escaped)
    else:
        # 문단 단위로 분할
        chunks = _chunk_by_paragraphs(body_escaped, max_body)
        messages.append(header + chunks[0])
        for chunk in chunks[1:]:
            messages.append(chunk)

    # 마지막 메시지: 상태 + 참고 기사
    footer_parts = [f"<i>({' | '.join(status_parts)})</i>"]
    if sources:
        footer_parts.append("\n──────────\n참고한 기사:")
        for s in sources:
            title = _escape_html(s.get("title", ""))
            url = s.get("url", "")
            footer_parts.append(f"- {title}\n  {url}")
    messages.append("\n".join(footer_parts))
    return messages


def _chunk_by_paragraphs(text: str, max_len: int) -> list[str]:
    """텍스트를 문단 단위로 분할한다."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        candidate = current + ("\n\n" if current else "") + p
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = p
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_len]]


def _escape_html(text: str) -> str:
    """Telegram HTML 파싱용 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

**기존 패턴 참고**: `format_article_message`의 HTML 포맷팅, `_split_blockquote_messages`의 분할 로직과 유사한 접근.

**_escape_html 중복 확인**: 기존 formatters.py에 이미 유사 함수가 있는지 확인. 있으면 재사용, 없으면 새로 추가. 기존 코드에서는 f-string 내에서 직접 HTML 태그를 사용하고 있으므로 _escape_html은 신규 추가.

---

## 테스트

### test_middleware.py

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def test_conversation_logger_text_message():
    """텍스트 메시지 로깅 테스트."""
    update = _make_update(text="타사 체크 해줘")
    context = _make_context()

    with patch("src.bot.middleware.save_conversation") as mock_save:
        mock_save.return_value = None
        from src.bot.middleware import conversation_logger
        await conversation_logger(update, context)

        mock_save.assert_called_once_with(
            context.bot_data["db"], "123", "user", "타사 체크 해줘", None, "text"
        )


async def test_conversation_logger_document():
    """파일 첨부 메시지 로깅 테스트."""
    doc = MagicMock()
    doc.file_id = "file123"
    doc.file_name = "test.pdf"
    doc.mime_type = "application/pdf"
    doc.file_size = 1024
    update = _make_update(text=None, caption="기사 써줘", document=doc)
    context = _make_context()

    with patch("src.bot.middleware.save_conversation") as mock_save:
        mock_save.return_value = None
        from src.bot.middleware import conversation_logger
        await conversation_logger(update, context)

        call_args = mock_save.call_args
        assert call_args[0][3] == "기사 써줘"  # content = caption
        assert call_args[0][4]["file_id"] == "file123"  # attachment_meta
        assert call_args[0][5] == "document"  # message_type


def _make_update(text=None, caption=None, document=None, photo=None):
    """테스트용 Update 목 생성."""
    update = MagicMock()
    update.effective_user.id = 123
    msg = MagicMock()
    msg.text = text
    msg.caption = caption
    msg.document = document
    msg.photo = photo
    update.effective_message = msg
    return update


def _make_context():
    """테스트용 Context 목 생성."""
    context = MagicMock()
    context.bot_data = {"db": MagicMock()}
    return context
```

### test_formatters_writing.py

```python
from src.bot.formatters import format_writing_result


def test_format_writing_result_basic():
    article = {
        "headline": "테스트 제목",
        "body": "테스트 본문입니다.",
        "word_count": 10,
        "sources": [{"title": "참고기사", "url": "https://example.com"}],
        "verified": "pass",
    }
    result = format_writing_result(article)
    assert len(result) >= 1
    assert "테스트 제목" in result[0]
    assert "참고한 기사" in result[-1] or "참고한 기사" in result[0]


def test_format_writing_result_no_sources():
    article = {
        "headline": "제목",
        "body": "본문",
        "word_count": 2,
        "sources": [],
        "verified": "skipped",
    }
    result = format_writing_result(article)
    assert "참고한 기사" not in result[0]
```

---

## 완료 기준

1. `middleware.py` 신규 생성, `conversation_logger`와 `tracked_reply` 구현
2. `config.py`에 8개 부서 `WRITING_STYLES` + `WRITING_STYLES_DEFAULT` 추가
3. `formatters.py`에 `format_writing_result` 추가 (HTML 포맷, 4096자 분할)
4. 기존 함수에 영향 없음
5. 테스트 통과
