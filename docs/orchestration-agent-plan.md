# Orchestration Agent 아키텍처 개선 계획

> 작성일: 2026-02-16
> 상태: Draft
> 대상: tasa-check v2 (현행 v1 기반 점진적 전환)

---

## 1. 현행 시스템 분석

### 1.1 현재 아키텍처
```
사용자 → /command → CommandHandler → Pipeline(절차적) → LLM(single-shot) → 응답
```

- **라우팅**: 명시적 커맨드 기반 (`/check`, `/report`, `/set_*`)
- **LLM 호출**: Single-shot + forced tool_use (에이전트 루프 없음)
- **대화 이력**: 저장하지 않음 (각 커맨드가 stateless)
- **첨부파일**: 미지원
- **모델**: Haiku 4.5 전용

### 1.2 인프라 제약
| 항목 | 현재 값 | 비고 |
|------|---------|------|
| 서버 RAM | 1GB | Oracle Cloud Free Tier |
| 동시 파이프라인 | 5 | `_pipeline_semaphore` |
| DB | SQLite (aiosqlite) | 단일 파일 |
| API 모델 | Haiku 4.5 | BYOK |
| 데이터 보관 | 5일 | `CACHE_RETENTION_DAYS` |

---

## 2. 제안 아키텍처

### 2.1 전체 흐름도
```
┌─────────────────────────────────────────────────────────────┐
│                    Telegram Update                          │
│  (text message / command / document / photo+caption)        │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│              Conversation Logger (미들웨어)                   │
│  모든 user 메시지 + bot 응답을 conversations 테이블에 저장      │
│  첨부파일: metadata만 저장 (file_id, name, mime, size)        │
└─────────────────┬───────────────────────────────────────────┘
                  │
          ┌───────┴───────┐
          │               │
    /command 직접     자연어 메시지
    (기존 핸들러)     (MessageHandler)
          │               │
          │               ▼
          │    ┌─────────────────────┐
          │    │    Pre-Callback     │
          │    │  ① 최근 3일 대화 로드  │
          │    │  ② 관련 대화 필터     │
          │    │  ③ 첨부파일 meta 추출 │
          │    └────────┬────────────┘
          │             ▼
          │    ┌─────────────────────┐
          │    │  Orchestration Agent│
          │    │  (LLM Router)      │
          │    │                    │
          │    │  Tools:            │
          │    │  - check           │
          │    │  - report          │
          │    │  - writing         │
          │    │  - schedule        │
          │    │  - set_division    │
          │    │  - set_keyword     │
          │    │  - reject          │
          │    └───┬──┬──┬──────────┘
          │        │  │  │
          ▼        ▼  │  ▼
     ┌────────┐  ┌────┘  ┌───────────────┐
     │ Check  │  │       │ Writing Agent │
     │Pipeline│  │       │ (Multi-tool)  │
     └────────┘  │       └───────────────┘
     ┌────────┐  │
     │ Report │  │
     │Pipeline│◄─┘
     └────────┘
```

### 2.2 핵심 설계 원칙

1. **하위 호환**: 기존 `/command` 핸들러는 그대로 유지. 자연어 입력만 Orchestrator 경유
2. **Single-shot Router**: Orchestrator는 1회 LLM 호출로 tool 1개를 결정 (비용 최소화)
3. **Writing Agent만 Multi-tool**: 유일하게 에이전트 루프를 가지는 컴포넌트
4. **Lazy File Loading**: 첨부파일 meta만 저장, 실제 다운로드는 Writing Agent가 필요할 때만
5. **Index-based LLM Output**: LLM은 항상 '번호'만 출력하고, 실제 콘텐츠 매핑은 코드가 수행. LLM이 전체 기사/대화 내용을 그대로 다시 출력하는 일이 없도록 설계하여 출력 토큰을 최소화

---

## 3. Telegram Bot API 검토 (첨부파일 & 대화 이력)

### 3.1 대화 이력 — Telegram은 제공하지 않음

> **핵심 제약**: Telegram Bot API는 과거 대화 이력 조회 API를 제공하지 않는다. Bot은 `getUpdates`/Webhook으로 실시간 수신하는 메시지만 볼 수 있다.

**대응**: 모든 메시지를 직접 DB에 저장해야 함 → `conversations` 테이블 + Logger 미들웨어 신설

```python
# python-telegram-bot v21 미들웨어 패턴
# Application.add_handler()의 group 파라미터로 우선순위 제어
app.add_handler(MessageHandler(filters.ALL, conversation_logger), group=-1)  # 최우선
```

**저장 대상**:
| 필드 | 설명 | 용량 추정 |
|------|------|----------|
| `role` | "user" / "assistant" | 10B |
| `content` | 메시지 본문 (최대 4096자) | ~4KB |
| `attachment_meta` | JSON: `{file_id, file_name, mime_type, file_size}` | ~200B |
| `message_type` | "text" / "document" / "photo" / "command" | 10B |

**용량 추정**: 사용자 10명 × 일 50메시지 × 4KB ≈ 2MB/일 → 3일 보관 = ~6MB (무시 가능)

### 3.2 첨부파일 처리 — Telegram getFile API

```
사용자가 파일 전송 → Bot이 message.document 수신
  ├── document.file_id      : 텔레그램 서버 파일 식별자 (재다운로드 가능)
  ├── document.file_name    : 원본 파일명
  ├── document.mime_type    : MIME 타입
  └── document.file_size    : 바이트 단위 크기
```

**Telegram Bot API 파일 제약**:
| 항목 | 제한 |
|------|------|
| 다운로드 최대 크기 | **20MB** (Bot API 제한) |
| file_id 유효기간 | **최소 1시간** 보장 (실제로는 수주간 유효) |
| 다운로드 방식 | HTTPS GET (getFile → file_path → download) |
| 동시 다운로드 | 제한 없음 (단, 서버 리소스 고려) |

**서비스 제한 (1GB RAM 고려)**:
| 항목 | 권장 값 | 사유 |
|------|---------|------|
| 파일 크기 상한 | **3MB** | 1GB RAM에서 파싱 시 메모리 3~5배 사용 가능 |
| 지원 형식 | PDF, DOCX, TXT | 기자 업무에 필요한 형식. HWP 미지원 |
| 이미지 OCR | **향후 고려** | OCR 라이브러리(Tesseract) 메모리 소비 大 |
| 동시 파일 처리 | **1건/사용자** | OOM 방지 |
| 파일 보관 | **즉시 삭제** | 다운로드 → 텍스트 추출 → 삭제 |

### 3.3 file_id 재사용 전략

Telegram의 `file_id`는 DB에 저장해두면 나중에 재다운로드가 가능하다.
- Writing Agent가 과거 첨부파일을 참조해야 할 때 `file_id`로 재다운로드
- 단, Telegram 서버에서 삭제되면 실패 → 에러 핸들링 필수
- 3일 이내 파일은 대부분 유효 (경험적으로 수주간 유지)

```python
# 재다운로드 패턴
try:
    file = await context.bot.get_file(stored_file_id)
    content = await file.download_as_bytearray()
except telegram.error.BadRequest:
    # file_id 만료 → 사용자에게 재전송 요청
    return "첨부파일이 만료되었습니다. 다시 전송해주세요."
```

### 3.4 첨부파일 단독 전송 처리

Telegram에서 사용자가 봇에게 파일을 보내는 방법은 2가지:

| 방식 | Telegram UX | Bot 수신 |
|------|------------|----------|
| **A: 파일+캡션** | 파일 첨부 → 캡션란에 텍스트 입력 → 전송 | 1개 Update: `document` + `caption` |
| **B: 파일만 먼저** | 파일만 전송 → 이어서 텍스트 메시지 | 2개 Update: `document`(캡션 없음), `text` |

모바일에서는 파일 첨부하면서 캡션을 쓰는 게 번거로우므로 **방식 B가 실제로 더 흔하다.**
방식 B에서 파일 단독 메시지가 도착하면 `query=""`이므로 orchestrator 라우팅이 불가하다.

**해결**: 파일 단독 메시지는 orchestrator를 경유하지 않고, 저장 + 안내 응답만 수행:

```python
async def orchestrator_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    query = message.text or message.caption or ""

    # ── 파일 단독 전송 (캡션 없음) ──
    if not query and (message.document or message.photo):
        doc = message.document
        if doc and doc.file_size > 3 * 1024 * 1024:
            await message.reply_text("파일 용량이 3MB를 초과합니다.")
            return
        if doc and _is_unsupported_mime(doc.mime_type):
            await message.reply_text(
                "지원하지 않는 파일 형식입니다. (PDF, DOCX, TXT만 지원)"
            )
            return
        await message.reply_text(
            "파일을 받았습니다. 어떻게 처리할까요?\n"
            "예) \"이 보도자료로 300자 기사 써줘\""
        )
        return  # Logger가 이미 저장 완료. orchestrator/pre-callback 미호출

    # ── 텍스트가 있는 경우 → 정상 orchestrator 흐름 ──
    # pre-callback이 직전 파일 메시지의 attachment_meta를 대화 이력에서 자동 포착
    context_data = await pre_callback(db, api_key, telegram_id, query)
    ...
```

**시나리오별 동작**:
| 시나리오 | 동작 |
|----------|------|
| 파일 + 캡션 "기사 써줘" | caption이 query → 즉시 pre-callback → orchestrator → writing |
| 파일만 전송 (캡션 없음) | 안내 응답: "파일을 받았습니다. 어떻게 처리할까요?" |
| → 이어서 "기사 써줘" | pre-callback이 직전 파일 메시지의 attachment_meta 포착 → writing |
| 텍스트만 "기사 써줘" (파일 없음) | pre-callback이 과거 대화에서 파일 탐색 → 있으면 writing, 없으면 키워드 기반 작성 |

### 3.5 MessageHandler 충돌 방지

현재 `ConversationHandler`들이 특정 상태에서 텍스트 입력을 기다린다 (`/set_keyword`, `/set_apikey`).
새로운 `MessageHandler(filters.TEXT & ~filters.COMMAND)` 추가 시 충돌 가능.

**해결**: `group` 파라미터와 `ConversationHandler` 우선순위 활용
```python
# 기존: group=0 (기본값)
app.add_handler(build_conversation_handler())    # /start, group=0
app.add_handler(build_settings_handler())        # /set_*, group=0

# 신규: group=1 (낮은 우선순위)
# ConversationHandler가 먼저 매칭 → fallthrough 시에만 orchestrator 실행
app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, orchestrator_handler),
    group=1,
)
app.add_handler(
    MessageHandler(filters.Document.ALL | filters.PHOTO, orchestrator_handler),
    group=1,
)
```

---

## 4. 컴포넌트 상세 설계

### 4.1 Conversation Logger (미들웨어)

**파일**: `src/bot/middleware.py` (신규)

**역할**: 모든 수신 메시지와 봇 응답을 `conversations` 테이블에 저장

```python
async def conversation_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """모든 메시지를 conversations 테이블에 기록한다."""
    message = update.effective_message
    if not message or not update.effective_user:
        return  # 콜백쿼리 등은 스킵

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
        # 가장 큰 해상도 선택
        photo = message.photo[-1]
        attachment_meta = {
            "file_id": photo.file_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": photo.file_size,
        }

    content = message.text or message.caption or ""
    message_type = _classify_message_type(message)

    await repo.save_conversation(db, telegram_id, "user", content, attachment_meta, message_type)
```

**봇 응답 기록**: `reply_text` 래퍼 함수로 응답도 자동 저장
```python
async def tracked_reply(original_reply, db, telegram_id, text, **kwargs):
    """reply_text를 감싸서 봇 응답도 conversations에 저장한다."""
    result = await original_reply(text, **kwargs)
    await repo.save_conversation(db, telegram_id, "assistant", text, None, "text")
    return result
```

### 4.2 Pre-Callback (LLM 기반 대화 필터링)

**파일**: `src/agents/orchestrator.py` (신규)

**역할**: Orchestrator 실행 전, LLM으로 관련 대화를 선별하여 context 구성

**핵심 원칙**: LLM은 대화 **번호**만 출력 → 코드가 DB에서 해당 대화의 전체 내용을 로드하여 context에 주입. 출력 토큰 최소화.

**전체 흐름**:
```
① DB에서 최근 3일 대화 로드 (최대 50건)
② 번호가 붙은 요약 목록 생성 (role + 첫 80자 + 첨부파일 표시)
③ LLM에 요약 목록 + 현재 쿼리 전달 → select_conversations tool_use
④ LLM이 관련 대화 번호만 출력 (출력 토큰: ~50)
⑤ 코드가 해당 번호의 대화 전체 내용을 DB에서 로드
⑥ 첨부파일 meta도 선별된 대화에서 추출
⑦ {relevant_messages, attachment_metas} 반환
```

**LLM 요약 목록 포맷** (입력):
```
[1] user 02-16 14:00 | "이 보도자료로 기사 써줘" [📎 보도자료.pdf 1.2MB]
[2] assistant 02-16 14:01 | "기사 작성 중입니다..."
[3] user 02-16 10:00 | "타사 체크 해줘"
[4] assistant 02-16 10:01 | "타사 체크 진행 중..."
...
[48] user 02-14 09:00 | "부서 경제부로 바꿔"
```

**Tool 정의**:
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
```

**시스템 프롬프트**:
```
아래 대화 목록에서 현재 사용자 요청과 관련된 대화 번호를 선별하라.
선별 기준:
1. 최근 3건은 항상 포함 (직전 맥락)
2. 현재 요청의 주제·키워드와 관련된 대화
3. 첨부파일이 있는 대화 (현재 요청이 기사 작성이면 특히 중요)
4. 관련 없는 대화는 제외하여 context 오염을 방지
select_conversations 도구로 번호만 제출하라.
```

**구현**:
```python
async def pre_callback(db, api_key: str, telegram_id: str, current_query: str) -> dict:
    """LLM으로 관련 대화를 선별하고, DB에서 전체 내용을 로드한다.

    Returns:
        {
            "relevant_messages": [...],    # 선별된 대화의 전체 내용
            "attachment_metas": [...],      # 선별된 대화 중 첨부파일 meta
        }
    """
    # ① 최근 3일 대화 로드
    conversations = await repo.get_recent_conversations(
        db, telegram_id, days=3, limit=50,
    )
    if not conversations:
        return {"relevant_messages": [], "attachment_metas": []}

    # ② 번호 붙은 요약 목록 생성 (LLM 입력용, 내용은 첫 80자만)
    summary_lines = []
    for i, c in enumerate(conversations, 1):
        truncated = c["content"][:80].replace("\n", " ")
        attach_tag = ""
        if c.get("attachment_meta"):
            meta = c["attachment_meta"]
            name = meta.get("file_name", "파일")
            size_mb = round(meta.get("file_size", 0) / 1_048_576, 1)
            attach_tag = f" [📎 {name} {size_mb}MB]"
        date_str = c["created_at"][5:16]  # "MM-DD HH:MM"
        summary_lines.append(f"[{i}] {c['role']} {date_str} | \"{truncated}\"{attach_tag}")

    summary_text = "\n".join(summary_lines)

    # ③④ LLM에 요약 목록 전달 → 관련 대화 번호만 출력
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)
    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,  # 번호만 출력하므로 적은 토큰으로 충분
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

    # ⑤ 선별된 번호로 DB에서 전체 대화 내용 로드
    selected_indices = []
    for block in message.content:
        if block.type == "tool_use" and block.name == "select_conversations":
            selected_indices = block.input.get("selected_indices", [])

    relevant = []
    for idx in selected_indices:
        if 1 <= idx <= len(conversations):
            relevant.append(conversations[idx - 1])  # 1-based → 0-based

    # ⑥ 선별된 대화에서 첨부파일 meta 추출
    attachment_metas = [
        {**c["attachment_meta"], "message_id": c["id"], "created_at": c["created_at"]}
        for c in relevant
        if c.get("attachment_meta")
    ]

    return {
        "relevant_messages": relevant,
        "attachment_metas": attachment_metas,
    }
```

**비용**: Haiku 4.5 × 1회 (입력 ~300토큰 요약 목록 + 출력 ~50토큰 번호 배열) ≈ $0.0005
**장점**: 규칙 기반 대비 관련성 판단 정확도 향상, 형태소 분석 의존성 불필요

### 4.3 Orchestration Agent (Router)

**파일**: `src/agents/orchestrator.py`

**역할**: 사용자 의도를 판단하여 tool 1개를 선택

**LLM 호출 방식**: Single-shot + forced tool_use (기존 패턴과 동일)

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
                    "enum": ["check", "report", "writing", "schedule", "set_division",
                             "set_keyword", "reject"],
                    "description": "실행할 도구"
                },
                "reason": {
                    "type": "string",
                    "description": "라우팅 판단 근거 (1문장)"
                },
                "extracted_params": {
                    "type": "object",
                    "description": "도구 실행에 필요한 파라미터 (예: writing의 주제, 분량 요청 등)"
                }
            },
            "required": ["tool", "reason"]
        }
    }
]
```

**시스템 프롬프트 (Router)**:
```
당신은 기자용 뉴스 서비스의 라우터입니다.
사용자의 메시지와 대화 맥락을 분석하여 적절한 도구 하나를 선택합니다.

사용 가능한 도구:
- check: 키워드 기반 타사 체크 (타 언론사 단독/주요 기사 모니터링)
- report: 부서 뉴스 브리핑 (부서 주요 뉴스 요약)
- writing: 기사 작성 (보도자료, 키워드, 첨부파일 기반 기사 초안 생성)
- schedule: 자동 실행 예약 설정
- set_division: 부서 변경
- set_keyword: 키워드 변경
- reject: 서비스 범위 밖 요청 (사유 명시)

reject 사유 예시:
- 제공하지 않는 기능입니다
- 첨부 파일의 용량이 3MB를 초과합니다
- 지원하지 않는 파일 형식입니다
```

**비용**: Haiku 4.5 × 1회 ≈ $0.001 (입력 ~500토큰 + 출력 ~100토큰)

**Orchestrator 입력 context 구성**: Pre-callback이 반환한 `relevant_messages`(전체 내용)와 `attachment_metas`를 Orchestrator 시스템 프롬프트에 주입. Pre-callback의 LLM이 선별한 대화만 포함되므로 context 오염이 방지됨.

### 4.4 Writing Tool Agent

**파일**: `src/agents/writing_agent.py` (신규)

**역할**: 기사 작성. 유일하게 multi-tool 에이전트 루프를 사용하는 컴포넌트

**모델**: Haiku 4.5 (전체 파이프라인 동일 모델)

**에이전트 루프 방식**:
```
Orchestrator → Writing Agent 호출
  └→ LLM이 tool 선택 (0~N개, 순차 실행)
     ├─ analyze_attachment → 첨부파일 다운로드 + 텍스트 추출 → context 추가
     ├─ fetch_articles → 네이버 검색 + 필터 → 번호 목록 반환 (제목+요약만)
     │    └→ LLM이 select_articles로 관련 기사 번호 선택
     │         └→ 코드가 선택된 기사만 본문 스크래핑 → context 추가
     ├─ get_writing_style → 부서 기본 스타일 가이드 로드 → context 추가
     └─ submit_article → 최종 기사 작성 결과 제출 (source_indices로 출처 참조)
```

**Index-based Output 원칙**: fetch_articles, submit_article 모두 LLM은 기사 '번호'만 출력. 전체 기사 내용을 LLM이 다시 출력하는 일이 없도록 설계.

**도구 정의**:

#### Tool 1: analyze_attachment
```python
{
    "name": "analyze_attachment",
    "description": "첨부파일을 열어 텍스트를 추출한다. attachment_metas에 파일이 있을 때만 사용.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_index": {
                "type": "integer",
                "description": "attachment_metas 배열의 인덱스 (0부터)"
            }
        },
        "required": ["file_index"]
    }
}
```

실행 시:
1. `attachment_metas[file_index]`에서 `file_id` 추출
2. `context.bot.get_file(file_id)` → 다운로드
3. MIME 타입에 따라 텍스트 추출:
   - `application/pdf` → `pymupdf` (fitz)
   - `application/vnd.openxmlformats...` → `python-docx`
   - `text/*` → 직접 읽기
   - 그 외 (HWP 등) → "지원하지 않는 파일 형식입니다" 반환
4. 추출된 텍스트를 LLM context에 추가
5. 임시 파일 즉시 삭제

#### Tool 2: fetch_articles (2단계 — 검색 + 번호 선택)

기존 check_agent의 `filter_news` → `submit_analysis` 패턴과 동일한 index-based 설계.
LLM이 기사 전문을 그대로 다시 출력하는 일이 없도록 2단계로 분리한다.

**Step A: fetch_articles** — 검색 + 필터 → 번호 목록만 LLM에 반환
```python
{
    "name": "fetch_articles",
    "description": "키워드로 네이버 뉴스를 검색한다. 결과는 번호 붙은 제목+요약 목록으로 반환된다. 필요한 기사를 select_articles로 선택하라.",
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "검색 키워드 (1~3개)"
            },
            "hours": {
                "type": "integer",
                "description": "검색 시간 범위 (시간 단위, 기본 24)",
                "default": 24
            }
        },
        "required": ["keywords"]
    }
}
```

실행 시 (서버 사이드):
1. `search_news(keywords, since, max_results=100)` (기존 모듈 재사용)
2. `filter_by_publisher()` (기존 모듈 재사용)
3. 광고/사진 기사 제목 필터 (기존 로직 재사용)
4. 부서 관련성 없는 기사 제외 (간단한 규칙 기반)
5. 결과를 내부 `_fetched_articles` 딕셔너리에 저장 (에이전트 루프 내 상태)
6. **LLM에 반환하는 tool_result**: 번호 + 제목 + description 첫 100자만
   ```
   [1] 조선일보 | 삼성전자 반도체 사업부 대규모 투자 발표 | 삼성전자가 16일 반도체 사업부에...
   [2] 한경 | SK하이닉스 HBM4 양산 본격화 | SK하이닉스는 차세대 고대역폭메모리...
   ...
   [15] 연합뉴스 | AI 반도체 수출 규제 강화 | 미국 상무부가 AI 반도체 수출 규제를...
   ```

**Step B: select_articles** — LLM이 관련 기사 번호 선택 → 코드가 본문 스크래핑
```python
{
    "name": "select_articles",
    "description": "fetch_articles 결과에서 기사 작성에 필요한 기사 번호를 선택한다. 선택된 기사의 본문이 context에 추가된다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "선택한 기사 번호 배열 (최대 10건)"
            }
        },
        "required": ["selected_indices"]
    }
}
```

실행 시 (서버 사이드):
1. `selected_indices`에 해당하는 기사 URL 추출 (`_fetched_articles`에서 역매핑)
2. 선택된 기사만 본문 스크래핑: `fetch_articles_batch(selected_urls)`
3. **LLM에 반환하는 tool_result**: 번호 + 제목 + 본문 전문 (context로 주입)
   ```
   [1] 조선일보 | 삼성전자 반도체 사업부 대규모 투자 발표
   본문: 삼성전자가 16일 반도체 사업부에 10조원 규모의 추가 투자를 단행한다고 발표했다...

   [3] 연합뉴스 | AI 반도체 수출 규제 강화
   본문: 미국 상무부가 AI용 반도체의 대중 수출 규제를 대폭 강화하는 행정명령에...
   ```

**토큰 절감 효과**: 15건 전체 본문(~15,000토큰) 대신 선택된 3~5건(~5,000토큰)만 context 사용. LLM 출력은 번호 배열(~20토큰)뿐.

#### Tool 3: get_writing_style
```python
{
    "name": "get_writing_style",
    "description": "기사 작성 스타일 가이드를 로드한다. 사용자가 언론사를 설정한 경우 해당 스타일, 미설정 시 부서 기본 스타일을 반환한다.",
    "input_schema": {
        "type": "object",
        "properties": {},
    }
}
```

실행 시:
1. DB `writing_styles` 테이블에서 해당 journalist의 스타일 조회
2. 레코드 있으면 → DB 저장 스타일 반환 (향후 언론사별 커스텀용)
3. 레코드 없으면 (기본) → `config.py`의 `WRITING_STYLES[department]` 부서 기본 가이드 반환
4. 포함 내용: 리드문 형식, 문단 구조, 톤, 금지 표현 등

> 현 단계에서는 언론사 선택 커맨드(`/set_style`)를 제공하지 않으므로 모든 사용자가 부서 기본 가이드를 사용. DB 테이블은 미리 생성하여 향후 확장에 대비.

#### Tool 4: submit_article (필수, 최종 제출)
```python
{
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
                "description": "참고한 기사 번호 (fetch_articles 기사 목록 번호)"
            }
        },
        "required": ["headline", "body", "word_count"]
    }
}
```

**source_indices 역매핑**: 코드가 `_fetched_articles`에서 번호 → URL 변환하여 최종 출력에 포함. LLM은 URL을 직접 출력하지 않음.

**에이전트 루프 제어**:
```python
MAX_TOOL_ITERATIONS = 5  # 최대 5회 tool 사용 (fetch→select→style→submit + 여유 1)

# 에이전트 루프 내 상태 (tool 실행 간 공유)
_fetched_articles: dict[int, dict] = {}  # 번호 → 기사 원본 데이터

WRITING_TOOLS = [
    _ANALYZE_ATTACHMENT_TOOL,
    _FETCH_ARTICLES_TOOL,
    _SELECT_ARTICLES_TOOL,
    _GET_WRITING_STYLE_TOOL,
    _SUBMIT_ARTICLE_TOOL,
]

async def run_writing_agent(api_key, context_data, bot_context):
    messages = [{"role": "user", "content": _build_writing_prompt(context_data)}]
    _fetched_articles.clear()

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            tools=WRITING_TOOLS,
            messages=messages,
        )

        # submit_article이 호출되면 종료
        if _has_submit(response):
            article = _extract_article(response)
            # source_indices → URL 역매핑
            article["source_urls"] = [
                _fetched_articles[i]["url"]
                for i in article.get("source_indices", [])
                if i in _fetched_articles
            ]
            return article

        # 다른 tool 호출 → 실행 → 결과를 messages에 추가
        # (fetch_articles → 요약 목록 반환, select_articles → 본문 반환)
        tool_results = await _execute_tools(response, bot_context, _fetched_articles)
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError("기사 작성 실패: 최대 반복 횟수 초과")
```

**전형적인 에이전트 루프 실행 시퀀스**:
```
Turn 1: LLM → analyze_attachment(0) + fetch_articles(["삼성전자 반도체"])
        → tool_result: 첨부파일 텍스트 + 기사 번호 목록 (제목+요약만)
Turn 2: LLM → select_articles([1, 3, 7])
        → tool_result: 선택된 기사 3건의 본문 전문
Turn 3: LLM → get_writing_style()
        → tool_result: 부서 스타일 가이드
Turn 4: LLM → submit_article(headline, body, word_count, source_indices=[1, 3, 7])
        → 완료. 코드가 source_indices를 URL로 역매핑
```

**기사 분량 기본값**:
- 사용자 미지정 시: 300~600자
- 사용자 지정 시: 최대 3000자
- Orchestrator의 `extracted_params`에서 분량 파라미터 전달

### 4.5 Check/Report Pipeline (기존 유지)

변경 없음. Orchestrator가 `tool=check` 또는 `tool=report` 결정 시 기존 `_run_check_pipeline()` / `_run_report_pipeline()` 호출.

단, Orchestrator 경유 시 `update.message` 대신 프로그래밍적으로 호출해야 하므로, 파이프라인 함수를 핸들러에서 분리하여 재사용 가능하게 리팩토링.

```python
# 현재: handlers.py에 파이프라인 + 메시지 전송이 결합
# 변경: 파이프라인 로직과 메시지 전송을 분리

# src/pipelines/check.py (리팩토링)
async def run_check(db, journalist) -> CheckResult:
    """순수 파이프라인 로직 (메시지 전송 없음)"""
    ...

# src/bot/handlers.py (기존)
async def check_handler(update, context):
    result = await run_check(db, journalist)
    await _send_check_results(update.message.reply_text, result)

# src/agents/orchestrator.py (신규)
async def _execute_check(db, journalist, send_fn):
    result = await run_check(db, journalist)
    await _send_check_results(send_fn, result)
```

---

## 5. DB 스키마 변경

### 5.1 신규 테이블: conversations
```sql
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    role TEXT NOT NULL,              -- 'user' | 'assistant'
    content TEXT NOT NULL DEFAULT '',
    attachment_meta TEXT,            -- JSON: {file_id, file_name, mime_type, file_size}
    message_type TEXT NOT NULL       -- 'text' | 'command' | 'document' | 'photo'
        DEFAULT 'text',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conv_journalist_created
    ON conversations(journalist_id, created_at);
```

### 5.2 신규 테이블: writing_styles

언론사별 커스텀 스타일 확장에 대비하여 DB 테이블을 미리 생성한다.
현 단계에서는 `/set_style` 커맨드를 제공하지 않으므로 테이블은 비어 있고, `get_writing_style` tool은 DB에 레코드가 없으면 `config.py`의 부서 기본 가이드로 fallback한다.

```sql
CREATE TABLE IF NOT EXISTS writing_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    publisher TEXT NOT NULL DEFAULT '',   -- 타겟 언론사 (빈 값 = 부서 기본)
    style_guide TEXT NOT NULL,            -- JSON: 작성 가이드
    example_articles TEXT DEFAULT '[]',   -- JSON: 예시 기사 배열
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(journalist_id, publisher)
);
```

**조회 로직**:
```python
async def get_writing_style(db, journalist_id: int, department: str) -> dict:
    """사용자 설정 스타일 → 없으면 부서 기본 가이드 반환."""
    cursor = await db.execute(
        "SELECT style_guide FROM writing_styles WHERE journalist_id = ? LIMIT 1",
        (journalist_id,),
    )
    row = await cursor.fetchone()
    if row:
        return json.loads(row["style_guide"])
    # fallback: config.py 부서 기본
    return WRITING_STYLES.get(department, WRITING_STYLES_DEFAULT)
```

**부서 기본 가이드** (`config.py`에 추가):
```python
WRITING_STYLES: dict[str, dict] = {
    "사회부": {
        "lead": "육하원칙 스트레이트. 첫 문장에 '누가 N일 무엇을 했다' 포함",
        "structure": "리드 → 핵심 팩트 → 배경 → 반응·전망",
        "tone": "객관적·건조체. '~했다' 종결",
        "forbidden": ["~것으로 알려졌다", "~관측이 나온다", "충격", "경악"],
        "length_default": "300~600자",
    },
    # ... 부서별 정의
}
```

### 5.3 cleanup 확장
```python
# conversations도 보관 기간 적용
await db.execute(
    "DELETE FROM conversations WHERE created_at < ?",
    (cutoff,),
)
```

---

## 6. 파일 구조 변경

```
src/
├── agents/
│   ├── check_agent.py          # 기존 (변경 없음)
│   ├── report_agent.py         # 기존 (변경 없음)
│   ├── orchestrator.py         # ✨ 신규: Router + Pre-callback
│   └── writing_agent.py        # ✨ 신규: 기사 작성 에이전트
├── bot/
│   ├── handlers.py             # 🔧 수정: 파이프라인 분리, orchestrator 핸들러 추가
│   ├── middleware.py            # ✨ 신규: Conversation Logger
│   ├── conversation.py         # 기존 (변경 없음)
│   ├── settings.py             # 기존 (변경 없음)
│   ├── formatters.py           # 🔧 수정: writing 출력 포맷 추가
│   └── scheduler.py            # 기존 (변경 없음)
├── tools/
│   ├── search.py               # 기존 (변경 없음, writing에서 재사용)
│   ├── scraper.py              # 기존 (변경 없음, writing에서 재사용)
│   └── file_parser.py          # ✨ 신규: 첨부파일 텍스트 추출
├── pipelines/                  # ✨ 신규 디렉토리
│   ├── __init__.py
│   ├── check.py                # 🔧 handlers.py에서 추출
│   └── report.py               # 🔧 handlers.py에서 추출
├── filters/
│   └── publisher.py            # 기존 (변경 없음)
├── storage/
│   ├── models.py               # 🔧 수정: conversations 테이블 DDL 추가
│   └── repository.py           # 🔧 수정: conversation CRUD 추가
└── config.py                   # 🔧 수정: writing 스타일 기본값 추가
```

신규 파일: 4개 (`orchestrator.py`, `writing_agent.py`, `middleware.py`, `file_parser.py`)
수정 파일: 5개 (`handlers.py`, `formatters.py`, `models.py`, `repository.py`, `config.py`)
구조 변경: `pipelines/` 디렉토리 신설 (handlers.py에서 파이프라인 로직 분리)

---

## 7. 의존성 추가

```toml
# pyproject.toml에 추가
dependencies = [
    # 기존 의존성 유지
    ...
    # 신규
    "pymupdf>=1.24.0",           # PDF 텍스트 추출 (C 확장, 빠르고 가벼움)
    "python-docx>=1.1.0",        # DOCX 텍스트 추출
]
```

**메모리 영향**:
- `pymupdf`: ~30MB (C 바인딩, 효율적)
- `python-docx`: ~5MB (순수 Python)
- 총 추가: ~35MB (1GB 중 3.5%)

**미지원 (추가하지 않음)**:
- `pytesseract` + `Pillow`: 이미지 OCR, ~100MB+, 향후 필요 시 검토
- `pyhwp`: HWP 파싱. 라이브러리 불안정하고 메모리 소비 大. 미지원 확정

---

## 8. 리소스 영향 분석

### 8.1 메모리 (1GB RAM)

| 항목 | 추가 메모리 | 비고 |
|------|------------|------|
| 의존성 | +35MB | pymupdf + python-docx |
| conversations DB 쿼리 | +2MB | 50건 × 4KB |
| 파일 파싱 (순간) | +9MB (3MB × 3) | 파일 크기 × ~3배 |
| 에이전트 루프 context | +5MB | multi-turn messages 누적 |
| **총 추가** | **~51MB** | 기존 대비 +5% |

### 8.2 API 비용 (BYOK, 전량 Haiku 4.5)

| 시나리오 | Haiku 호출 | 추가 비용 | 내역 |
|----------|-----------|----------|------|
| 자연어 → check | +2 | +$0.0015 | pre-callback(1) + routing(1) |
| 자연어 → report | +2 | +$0.0015 | pre-callback(1) + routing(1) |
| 자연어 → writing (full) | +2 + 4~5 | +$0.005~0.008 | pre-callback(1) + routing(1) + agent loop(4~5) |
| /check (기존 커맨드) | 0 | $0 | 직접 핸들러, orchestrator 미경유 |

**Index-based 출력 토큰 절감 효과**:
- Pre-callback: 대화 50건 전체 내용 대신 번호 배열 출력 → 출력 ~50토큰 (vs 전체 반환 시 ~2,000토큰)
- fetch_articles → select_articles: 기사 15건 전문 대신 번호 배열 → 출력 ~20토큰 (vs 전체 반환 시 ~5,000토큰)
- submit_article: source_indices 번호만 → URL 역매핑은 코드가 처리

### 8.3 동시성

```python
_pipeline_semaphore = asyncio.Semaphore(5)    # 기존 유지
_writing_semaphore = asyncio.Semaphore(2)     # 신규: writing은 더 무거움
_file_parse_semaphore = asyncio.Semaphore(1)  # 신규: 파일 파싱은 1건씩
```

---

## 9. 개발 단계

### Phase 0: 기반 인프라 (2~3일)

| 작업 | 파일 | 설명 |
|------|------|------|
| conversations 테이블 DDL | `models.py` | 스키마 추가 + 마이그레이션 |
| conversation CRUD | `repository.py` | save / get_recent / cleanup |
| Conversation Logger | `middleware.py` | 미들웨어 핸들러 |
| main.py 미들웨어 등록 | `main.py` | group=-1 핸들러 추가 |
| 테스트 | `tests/` | 미들웨어 + repository 테스트 |

**완료 기준**: 모든 메시지가 DB에 저장되고, 3일 초과 데이터가 자동 정리됨

### Phase 1: Orchestration Agent (3~4일)

| 작업 | 파일 | 설명 |
|------|------|------|
| Pre-callback 구현 | `orchestrator.py` | LLM 기반 대화 필터 (select_conversations tool) |
| Router Agent 구현 | `orchestrator.py` | LLM 라우팅 (route_to_tool, single-shot tool_use) |
| 파이프라인 분리 | `pipelines/check.py`, `pipelines/report.py` | handlers.py에서 로직 추출 |
| Orchestrator 핸들러 | `handlers.py` | MessageHandler 등록 |
| reject 응답 처리 | `orchestrator.py` | 미지원 기능, 잘못된 요청 즉시 응답 |
| 테스트 | `tests/` | pre-callback 필터 정확도 + 라우팅 정확도 테스트 |

**완료 기준**: "타사 체크 해줘" → pre-callback → routing → check 파이프라인 실행

### Phase 2: Writing Agent (4~5일)

| 작업 | 파일 | 설명 |
|------|------|------|
| file_parser 구현 | `file_parser.py` | PDF, DOCX, TXT 텍스트 추출 (HWP 미지원) |
| writing_agent 구현 | `writing_agent.py` | 에이전트 루프 + 5개 tool |
| fetch + select_articles | `writing_agent.py` | 2단계 index-based 기사 수집 (검색→번호선택→스크래핑) |
| writing style 시스템 | `models.py`, `repository.py`, `config.py` | DB 테이블 DDL + 조회 로직 + 부서별 기본 가이드 fallback |
| submit_article 역매핑 | `writing_agent.py` | source_indices → URL 역매핑 로직 |
| 기사 출력 포매터 | `formatters.py` | 기사 Telegram HTML 포맷 |
| 테스트 | `tests/` | 파일 파싱 + index-based 루프 + 역매핑 테스트 |

**완료 기준**: "이 보도자료로 기사 써줘" + PDF → 300~600자 기사 생성 + 출처 URL 포함

### Phase 3: 통합 & 안정화 (2~3일)

| 작업 | 파일 | 설명 |
|------|------|------|
| Orchestrator ↔ Writing 연동 | `orchestrator.py` | routing → writing agent 호출 |
| Orchestrator ↔ Check/Report 연동 | `orchestrator.py` | routing → 기존 파이프라인 호출 |
| 에러 핸들링 통합 | `handlers.py` | 모든 경로의 에러 → 사용자 친화적 메시지 |
| E2E 테스트 | `tests/` | 전체 흐름 통합 테스트 |
| 부하 테스트 | `scripts/` | 동시 사용자 시뮬레이션 |
| 봇 명령어 목록 업데이트 | `main.py` | set_my_commands 갱신 |

**완료 기준**: 자연어 + 커맨드 + 첨부파일 모두 정상 동작

### Phase 4 (향후): 고도화

- [ ] 이미지 OCR 지원 (Tesseract)
- [ ] 언론사별 커스텀 스타일: `/set_style` 커맨드 추가 (DB 테이블은 이미 생성 완료)
- [ ] 기사 수정/편집 (이전 결과 참조, 대화 맥락 기반 follow-up)
- [ ] 모델 선택 옵션 (/set_model로 Haiku/Sonnet 전환)

---

## 10. 위험 요소 & 대응

| 위험 | 심각도 | 확률 | 대응 |
|------|--------|------|------|
| Haiku 라우팅 오분류 | 중 | 중 | 기존 /command 유지 (fallback), 라우팅 로그 모니터링 |
| Pre-callback LLM 필터 누락 | 중 | 중 | 최근 3건은 프롬프트에서 항상 포함 지시. 필터 결과 로그 모니터링 |
| Pre-callback LLM 호출 실패 | 중 | 저 | fallback: 최근 5건 + 첨부파일 대화 전부 포함 (규칙 기반) |
| 1GB RAM에서 파일 파싱 OOM | 고 | 저 | 3MB 제한, 동시 1건, 즉시 GC |
| file_id 만료로 재다운로드 실패 | 저 | 저 | 에러 메시지 + 재전송 요청 |
| Writing agent 무한 루프 | 중 | 저 | MAX_TOOL_ITERATIONS=5, 타임아웃 60초 |
| select_articles 번호 오매핑 | 저 | 저 | 범위 검증 (1~N), 무효 번호 무시 |
| ConversationHandler 충돌 | 중 | 중 | group 우선순위 분리, 통합 테스트 |
| conversations 테이블 용량 증가 | 저 | 저 | 3일 보관 + cleanup, 10유저 기준 ~6MB |
| 기존 /check, /report 동작 변경 | 고 | 저 | 기존 CommandHandler 그대로 유지 |

---

## 11. 테스트 전략

### 11.1 단위 테스트
- `test_middleware.py`: 메시지 로깅, 첨부파일 meta 추출
- `test_orchestrator.py`: pre-callback LLM 필터 (select_conversations mock), 라우팅 정확도 (route_to_tool mock)
- `test_writing_agent.py`: 에이전트 루프, fetch→select 2단계, source_indices 역매핑 (mock LLM + mock tools)
- `test_file_parser.py`: PDF/DOCX/TXT 텍스트 추출, 미지원 형식(HWP) 에러 처리

### 11.2 라우팅 정확도 테스트
```python
ROUTING_TEST_CASES = [
    ("오늘 타사 기사 좀 봐줘", "check"),
    ("브리핑 줘", "report"),
    ("이 보도자료로 기사 써줘", "writing"),
    ("매일 9시에 체크 돌려줘", "schedule"),
    ("부서 경제부로 바꿔", "set_division"),
    ("키워드 삼성전자 추가해줘", "set_keyword"),
    ("날씨 알려줘", "reject"),
    ("주식 추천해줘", "reject"),
]
```

### 11.3 통합 테스트
- 자연어 → Orchestrator → Check 파이프라인 전체 흐름
- 첨부파일 + 자연어 → Orchestrator → Writing Agent 전체 흐름
- 기존 /check, /report 명령이 여전히 정상 동작하는지 회귀 테스트

---

## 12. 결론

### 실현 가능성: **높음**

- 기존 아키텍처를 크게 변경하지 않고 **점진적 확장** 가능
- 핵심 제약(1GB RAM, BYOK, SQLite)과 충돌하지 않음
- 기존 `/command` 핸들러를 유지하여 **하위 호환 보장**
- Haiku 4.5로 라우팅 충분 (분류 태스크는 Haiku의 강점)

### 확정된 설계 결정

| 항목 | 결정 | 사유 |
|------|------|------|
| Writing 모델 | **Haiku 4.5** | 전체 파이프라인 동일 모델, BYOK 비용 최소화 |
| HWP 지원 | **미지원** | 라이브러리 불안정, 메모리 소비 大 |
| 스타일 가이드 | **DB 테이블 선 생성** + config.py fallback | 현 단계 부서 기본만 사용. 테이블은 향후 언론사별 확장에 대비 |
| Pre-callback 필터링 | **LLM 기반** (번호만 출력) | 규칙 기반 대비 정확도 향상, 형태소 분석 의존성 불필요 |
| LLM 출력 최적화 | **Index-based** | 모든 LLM 출력에서 콘텐츠 재출력 방지, 번호만 출력 후 코드가 역매핑 |
