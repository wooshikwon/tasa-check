# E: Writing Agent 구현 가이드

> 담당: 기사 작성 에이전트 (multi-tool agent loop) + 파일 파서 + 팩트 체크

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/agents/writing_agent.py` | 신규 생성 — 에이전트 루프, 5개 tool, verification |
| `src/tools/file_parser.py` | 신규 생성 — PDF, DOCX, TXT 텍스트 추출 |

## 금지 사항

- 기존 agents/ 파일 수정 금지 (check_agent.py, report_agent.py)
- handlers.py, main.py, repository.py 수정 금지
- 기존 tools/ 파일 수정 금지 (search.py, scraper.py)

---

## 외부 인터페이스 의존성

```python
# 기존 코드 (변경 없음)
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name

# Storage Layer (Agent A)
from src.storage.repository import get_writing_style, get_journalist

# 자체 모듈
from src.tools.file_parser import extract_text
```

---

## 노출 인터페이스

```python
async def run_writing_agent(
    api_key: str, context_data: dict, bot_context,
) -> dict
# context_data: pre_callback 반환값 {relevant_messages, attachment_metas}
# bot_context: telegram ContextTypes.DEFAULT_TYPE (파일 다운로드용)
# 반환: {headline, body, word_count, sources, verified, verification_issues}
```

---

## 상세 구현

### 1. src/tools/file_parser.py (신규)

```python
"""첨부파일 텍스트 추출.

지원 형식: PDF, DOCX, TXT
메모리 보호를 위해 동시 1건만 처리.
"""

import asyncio

_file_parse_semaphore = asyncio.Semaphore(1)

_MAX_TEXT_LENGTH = 10_000  # 추출 텍스트 최대 길이


async def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """바이트 데이터에서 텍스트를 추출한다.

    Args:
        file_bytes: 파일 바이너리 데이터
        mime_type: MIME 타입

    Returns:
        추출된 텍스트 (최대 10,000자)

    Raises:
        ValueError: 미지원 파일 형식
    """
    async with _file_parse_semaphore:
        # 동기 파싱을 asyncio.to_thread로 비동기 실행
        return await asyncio.to_thread(_extract_sync, file_bytes, mime_type)


def _extract_sync(file_bytes: bytes, mime_type: str) -> str:
    """동기 텍스트 추출."""
    if mime_type == "application/pdf":
        return _extract_pdf(file_bytes)
    elif mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return _extract_docx(file_bytes)
    elif mime_type and mime_type.startswith("text/"):
        return file_bytes.decode("utf-8", errors="replace")[:_MAX_TEXT_LENGTH]
    else:
        raise ValueError(f"지원하지 않는 파일 형식입니다: {mime_type}")


def _extract_pdf(file_bytes: bytes) -> str:
    """PDF에서 텍스트 추출. pymupdf 사용."""
    import fitz  # pymupdf

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()
    text = "\n".join(text_parts)
    return text[:_MAX_TEXT_LENGTH]


def _extract_docx(file_bytes: bytes) -> str:
    """DOCX에서 텍스트 추출. python-docx 사용."""
    import io
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    text_parts = [para.text for para in doc.paragraphs if para.text.strip()]
    text = "\n".join(text_parts)
    return text[:_MAX_TEXT_LENGTH]
```

**의존성**: `pymupdf>=1.24.0`, `python-docx>=1.1.0` (pyproject.toml에 추가 필요 — Integration에서 처리)

### 2. src/agents/writing_agent.py (신규)

#### 파일 헤더 + 상수

```python
"""기사 작성 에이전트.

유일하게 multi-tool 에이전트 루프를 사용하는 컴포넌트.
5개 tool을 자율적으로 선택하여 기사를 작성하고,
에이전트 루프 종료 후 별도 LLM 호출로 팩트 체크를 수행한다.
"""

import asyncio
import json
from datetime import datetime, timedelta

import anthropic

from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.storage.repository import get_writing_style, get_journalist
from src.tools.file_parser import extract_text

MAX_TOOL_ITERATIONS = 5
```

#### Tool 정의 (5개)

```python
_ANALYZE_ATTACHMENT_TOOL = {
    "name": "analyze_attachment",
    "description": "첨부파일을 열어 텍스트를 추출한다. attachment_metas에 파일이 있을 때만 사용.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_index": {
                "type": "integer",
                "description": "attachment_metas 배열의 인덱스 (0부터)",
            }
        },
        "required": ["file_index"],
    },
}

_FETCH_ARTICLES_TOOL = {
    "name": "fetch_articles",
    "description": "키워드로 네이버 뉴스를 검색한다. 결과는 번호 붙은 제목+요약 목록으로 반환된다. 필요한 기사를 select_articles로 선택하라.",
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "검색 키워드 (1~3개)",
            },
            "hours": {
                "type": "integer",
                "description": "검색 시간 범위 (기본 24)",
                "default": 24,
            },
        },
        "required": ["keywords"],
    },
}

_SELECT_ARTICLES_TOOL = {
    "name": "select_articles",
    "description": "fetch_articles 결과에서 기사 작성에 필요한 기사 번호를 선택한다. 선택된 기사의 본문이 context에 추가된다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "선택한 기사 번호 배열 (최대 10건)",
            }
        },
        "required": ["selected_indices"],
    },
}

_GET_WRITING_STYLE_TOOL = {
    "name": "get_writing_style",
    "description": (
        "부서 스타일 가이드와 예시 기사를 로드한다. "
        "스타일 규칙(리드문, 구조, 톤, 금지 표현)과 부서별 예시 기사 5건을 반환한다. "
        "예시 기사의 문장 스타일, 논리 전개 구조, 표현 방식(숫자·날짜 작성법 등)을 분석하여 기사 작성에 반영하라."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

_SUBMIT_ARTICLE_TOOL = {
    "name": "submit_article",
    "description": "작성된 기사를 제출한다. 참고 기사는 URL이 아닌 번호로 참조한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "기사 제목"},
            "body": {"type": "string", "description": "기사 본문"},
            "word_count": {"type": "integer", "description": "글자 수"},
            "source_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "참고한 기사 번호 (fetch_articles 기사 목록 번호)",
            },
        },
        "required": ["headline", "body", "word_count"],
    },
}

WRITING_TOOLS = [
    _ANALYZE_ATTACHMENT_TOOL,
    _FETCH_ARTICLES_TOOL,
    _SELECT_ARTICLES_TOOL,
    _GET_WRITING_STYLE_TOOL,
    _SUBMIT_ARTICLE_TOOL,
]
```

#### 시스템 프롬프트

```python
def _build_system_prompt(context_data: dict, params: dict) -> str:
    """Writing Agent 시스템 프롬프트를 구성한다."""
    parts = [
        "당신은 기자를 보조하는 기사 작성 에이전트입니다.",
        "사용 가능한 도구를 활용하여 기사를 작성하세요.",
        "",
        "작성 절차:",
        "1. 첨부파일이 있으면 analyze_attachment로 텍스트를 추출한다",
        "2. fetch_articles로 관련 뉴스를 검색한다",
        "3. select_articles로 참고할 기사를 선택한다",
        "4. get_writing_style로 스타일 가이드와 예시 기사를 로드한다",
        "5. submit_article로 최종 기사를 제출한다",
        "",
        "예시 기사 활용법:",
        "- 예시 기사의 문장 스타일(길이, 종결어미, 인용 방식)을 분석하여 유사하게 작성",
        "- 논리 전개 구조(리드→팩트→배경→전망 등)를 따라 기사를 구성",
        "- 숫자·날짜 표기 방식(27조1000억원, 12.1%, 18일)을 동일하게 사용",
        "",
        "submit_article 시 source_indices에 참고한 기사 번호를 반드시 포함하라.",
        "기사 내용은 원본 자료(첨부파일, 참고 기사)에 근거해야 하며 추측을 포함하지 않는다.",
    ]

    # 첨부파일 정보
    attachments = context_data.get("attachment_metas", [])
    if attachments:
        parts.append(f"\n첨부파일 {len(attachments)}건이 대화에 있습니다. analyze_attachment로 확인하세요.")

    # Orchestrator가 추출한 파라미터
    if params.get("topic"):
        parts.append(f"\n주제: {params['topic']}")
    if params.get("word_count"):
        parts.append(f"요청 분량: {params['word_count']}자")
    else:
        parts.append("기본 분량: 300~600자")
    if params.get("search_keywords"):
        parts.append(f"검색 키워드 힌트: {', '.join(params['search_keywords'])}")
    if params.get("style_hint"):
        parts.append(f"스타일: {params['style_hint']}")

    return "\n".join(parts)
```

#### 에이전트 루프

```python
async def run_writing_agent(
    api_key: str, context_data: dict, bot_context,
) -> dict:
    """기사 작성 에이전트 루프를 실행한다.

    Args:
        api_key: Anthropic API 키 (BYOK)
        context_data: pre_callback 반환값
        bot_context: telegram context (파일 다운로드용)

    Returns:
        {headline, body, word_count, sources, verified, verification_issues}
    """
    params = context_data.get("extracted_params", {})
    journalist = context_data.get("journalist", {})

    system_prompt = _build_system_prompt(context_data, params)
    user_prompt = _build_user_prompt(context_data)
    messages = [{"role": "user", "content": user_prompt}]

    # 에이전트 루프 내 상태
    fetched_articles: dict[int, dict] = {}  # 번호 → 기사 원본 데이터
    attachment_text: str | None = None

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_prompt,
            tools=WRITING_TOOLS,
            messages=messages,
        )

        # submit_article이 호출되면 루프 종료
        article = _extract_submit(response)
        if article:
            # source_indices → {title, url} 역매핑
            article["sources"] = [
                {"title": fetched_articles[i]["title"], "url": fetched_articles[i]["url"]}
                for i in article.get("source_indices", [])
                if i in fetched_articles
            ]
            # 팩트 체크 (에이전트 루프 밖)
            article = await _verify_article(
                api_key, article,
                sources=fetched_articles,
                attachment_text=attachment_text,
            )
            return article

        # tool 호출 처리
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            result = await _execute_tool(
                block.name, block.input,
                context_data=context_data,
                bot_context=bot_context,
                fetched_articles=fetched_articles,
                journalist=journalist,
            )

            # attachment 텍스트 보존 (verification용)
            if block.name == "analyze_attachment" and not result.startswith("오류"):
                attachment_text = result

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError("기사 작성 실패: 최대 반복 횟수 초과")


def _build_user_prompt(context_data: dict) -> str:
    """대화 맥락에서 사용자 요청을 구성한다."""
    messages = context_data.get("relevant_messages", [])
    parts = []
    for m in messages[-5:]:
        if m["role"] == "user":
            parts.append(m["content"])
    return "\n".join(parts) if parts else "기사를 작성해주세요."


def _extract_submit(response) -> dict | None:
    """response에서 submit_article 호출을 추출한다."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_article":
            return dict(block.input)
    return None
```

#### Tool 실행 함수

```python
async def _execute_tool(
    tool_name: str,
    tool_input: dict,
    context_data: dict,
    bot_context,
    fetched_articles: dict,
    journalist: dict,
) -> str:
    """개별 tool을 실행하고 결과 문자열을 반환한다."""

    if tool_name == "analyze_attachment":
        return await _tool_analyze_attachment(tool_input, context_data, bot_context)

    elif tool_name == "fetch_articles":
        return await _tool_fetch_articles(tool_input, fetched_articles)

    elif tool_name == "select_articles":
        return await _tool_select_articles(tool_input, fetched_articles)

    elif tool_name == "get_writing_style":
        return await _tool_get_writing_style(journalist, bot_context)

    return f"알 수 없는 도구: {tool_name}"


async def _tool_analyze_attachment(tool_input, context_data, bot_context) -> str:
    """첨부파일 다운로드 + 텍스트 추출."""
    file_index = tool_input.get("file_index", 0)
    metas = context_data.get("attachment_metas", [])
    if file_index >= len(metas):
        return "오류: 첨부파일 인덱스 범위 초과"

    meta = metas[file_index]
    file_id = meta.get("file_id")
    mime_type = meta.get("mime_type", "")

    try:
        file = await bot_context.bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()
        text = await extract_text(bytes(file_bytes), mime_type)
        return text
    except ValueError as e:
        return f"오류: {e}"
    except Exception as e:
        return f"오류: 파일 다운로드 실패 - {e}"


async def _tool_fetch_articles(tool_input, fetched_articles) -> str:
    """네이버 뉴스 검색 + 번호 목록 반환."""
    keywords = tool_input.get("keywords", [])
    hours = tool_input.get("hours", 24)
    since = datetime.now() - timedelta(hours=hours)

    raw = await search_news(keywords, since, max_results=100)
    filtered = filter_by_publisher(raw)

    if not filtered:
        return "검색 결과가 없습니다."

    # 번호 부여 + 내부 저장
    fetched_articles.clear()
    lines = []
    for i, article in enumerate(filtered[:30], 1):  # 최대 30건
        publisher = get_publisher_name(article.get("originallink", "")) or "알 수 없음"
        title = article.get("title", "")
        desc = article.get("description", "")[:100]
        fetched_articles[i] = {
            "title": title,
            "url": article.get("originallink") or article.get("link", ""),
            "description": desc,
            "publisher": publisher,
        }
        lines.append(f"[{i}] {publisher} | {title} | {desc}")

    return "\n".join(lines)


async def _tool_select_articles(tool_input, fetched_articles) -> str:
    """선택된 기사 본문 스크래핑."""
    indices = tool_input.get("selected_indices", [])
    valid_indices = [i for i in indices if i in fetched_articles]

    if not valid_indices:
        return "유효한 기사 번호가 없습니다."

    urls = {fetched_articles[i]["url"]: i for i in valid_indices}
    bodies = await fetch_articles_batch(list(urls.keys()))

    lines = []
    for url, idx in urls.items():
        article = fetched_articles[idx]
        body = bodies.get(url)
        if body:
            fetched_articles[idx]["body"] = body
            lines.append(f"[{idx}] {article['publisher']} | {article['title']}\n본문: {body}")
        else:
            lines.append(f"[{idx}] {article['publisher']} | {article['title']}\n본문: (스크래핑 실패)")

    return "\n\n".join(lines)


async def _tool_get_writing_style(journalist, bot_context) -> str:
    """스타일 가이드 + 예시 기사 로드."""
    db = bot_context.bot_data["db"]
    department = journalist.get("department", "사회부")
    journalist_id = journalist.get("id", 0)

    style = await get_writing_style(db, journalist_id, department)
    rules = style.get("rules", {})
    examples = style.get("examples", [])

    parts = ["[스타일 규칙]"]
    for key, value in rules.items():
        if isinstance(value, list):
            parts.append(f"- {key}: {', '.join(value)}")
        else:
            parts.append(f"- {key}: {value}")

    for i, example in enumerate(examples, 1):
        # 예시 기사 앞부분 (전체 포함, LLM이 스타일 학습)
        parts.append(f"\n[예시 기사 {i}]")
        parts.append(example)

    return "\n".join(parts)
```

#### Verification (팩트 체크)

```python
_VERIFY_ARTICLE_TOOL = {
    "name": "verify_article",
    "description": "작성된 기사의 사실관계를 원본 자료와 대조하여 검증한다",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string", "description": "기사 내 각 주장을 원본과 대조한 과정"},
            "verdict": {"type": "string", "enum": ["pass", "needs_revision"]},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string", "description": "기사 내 주장"},
                        "status": {"type": "string", "enum": ["confirmed", "not_found", "contradicted"]},
                        "source": {"type": "string", "description": "확인/미확인 출처"},
                    },
                    "required": ["claim", "status", "source"],
                },
            },
            "revised_body": {"type": "string", "description": "needs_revision일 때 수정 본문. pass이면 빈 문자열"},
        },
        "required": ["thinking", "verdict", "issues", "revised_body"],
    },
}

_VERIFY_SYSTEM_PROMPT = """당신은 팩트체커입니다. 아래 기사의 모든 사실적 주장을 원본 자료와 대조하라.

검증 기준:
1. 인물명, 기관명, 수치, 날짜, 인용문은 원본과 정확히 일치해야 한다
2. 원본에 없는 인과관계, 평가, 전망을 추가하지 않았는지 확인한다
3. 원본의 맥락을 왜곡하는 재구성이 없는지 확인한다
4. 원본에서 확인되지 않는 주장은 not_found로 표기한다

verdict 판단:
- pass: 모든 주장이 confirmed
- needs_revision: not_found 또는 contradicted가 1건 이상 → revised_body에 해당 부분을 삭제하거나 원본에 근거한 내용으로 교체

verify_article 도구로 결과를 제출하라."""


async def _verify_article(
    api_key: str,
    article: dict,
    sources: dict[int, dict],
    attachment_text: str | None,
) -> dict:
    """생성된 기사를 원본 자료와 대조하여 검증한다."""
    # 원본 자료 조립
    source_parts = []
    for idx in article.get("source_indices", []):
        if idx in sources:
            s = sources[idx]
            source_parts.append(f"[기사 {idx}] {s['title']}\n{s.get('body', '')}")
    if attachment_text:
        source_parts.append(f"[첨부파일]\n{attachment_text}")
    source_text = "\n\n".join(source_parts)

    if not source_text:
        article["verified"] = "skipped"
        article["verification_issues"] = []
        return article

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            temperature=0.0,
            system=_VERIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": (
                f"<article>\n제목: {article['headline']}\n{article['body']}\n</article>\n\n"
                f"<sources>\n{source_text}\n</sources>"
            )}],
            tools=[_VERIFY_ARTICLE_TOOL],
            tool_choice={"type": "tool", "name": "verify_article"},
        )

        for block in message.content:
            if block.type == "tool_use" and block.name == "verify_article":
                result = block.input
                if result["verdict"] == "needs_revision" and result.get("revised_body"):
                    article["body"] = result["revised_body"]
                    article["verified"] = "revised"
                else:
                    article["verified"] = "pass"
                article["verification_issues"] = result.get("issues", [])
                return article
    except Exception:
        pass

    article["verified"] = "skipped"
    article["verification_issues"] = []
    return article
```

---

## 기존 코드 패턴 참고

- **forced tool_use**: `tool_choice={"type": "tool", "name": "..."}` — check_agent.py 동일
- **search_news**: `search_news(keywords, since, max_results)` → list[dict] (title, link, originallink, description, pubDate)
- **filter_by_publisher**: `filter_by_publisher(articles)` → list[dict] (화이트리스트 언론사만)
- **fetch_articles_batch**: `fetch_articles_batch(urls)` → dict[url, body_text|None] (최대 800자)
- **get_publisher_name**: `get_publisher_name(url)` → str|None

---

## 테스트

`tests/test_writing_agent.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def test_extract_text_pdf():
    """PDF 텍스트 추출 (pymupdf 설치 필요)."""
    # 간단한 PDF 바이트가 필요하므로 실제 테스트는 fixture 사용
    ...


async def test_extract_text_unsupported():
    """미지원 파일 형식 ValueError."""
    from src.tools.file_parser import extract_text
    with pytest.raises(ValueError, match="지원하지 않는"):
        await extract_text(b"data", "application/x-hwp")


async def test_tool_fetch_articles():
    """fetch_articles tool이 번호 목록을 반환하는지 확인."""
    with patch("src.agents.writing_agent.search_news") as mock_search, \
         patch("src.agents.writing_agent.filter_by_publisher") as mock_filter:
        mock_search.return_value = [
            {"title": "테스트", "originallink": "https://example.com", "description": "설명", "link": "https://example.com"},
        ]
        mock_filter.return_value = mock_search.return_value

        from src.agents.writing_agent import _tool_fetch_articles
        fetched = {}
        result = await _tool_fetch_articles({"keywords": ["테스트"]}, fetched)
        assert "[1]" in result
        assert 1 in fetched


async def test_source_indices_mapping():
    """source_indices → {title, url} 역매핑 테스트."""
    fetched = {
        1: {"title": "기사1", "url": "https://a.com"},
        3: {"title": "기사3", "url": "https://b.com"},
    }
    article = {"source_indices": [1, 3], "headline": "test", "body": "test"}
    article["sources"] = [
        {"title": fetched[i]["title"], "url": fetched[i]["url"]}
        for i in article["source_indices"]
        if i in fetched
    ]
    assert len(article["sources"]) == 2
    assert article["sources"][0]["title"] == "기사1"
```

---

## 완료 기준

1. `file_parser.py` 신규 생성: PDF, DOCX, TXT 추출, 미지원 형식 ValueError
2. `writing_agent.py` 신규 생성: 에이전트 루프 + 5개 tool + verification
3. `run_writing_agent` 시그니처가 인터페이스 계약과 일치
4. fetch_articles → select_articles 2단계 index-based 동작
5. get_writing_style이 예시 기사 포함한 결과 반환
6. submit_article의 source_indices → {title, url} 역매핑 정상
7. verification이 에이전트 루프 밖에서 실행, pass/needs_revision 분기
8. 테스트 통과
