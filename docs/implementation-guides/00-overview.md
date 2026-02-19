# Sub-Agent 병렬 구현 전략

> 이 문서는 `docs/orchestration-agent-plan.md`를 병렬 sub-agent로 구현하기 위한 전략이다.
> 각 가이드(`01`~`06`)는 독립적으로 읽을 수 있도록 작성되어 있으며, sub-agent는 자신의 가이드만 참조하면 된다.

---

## 의존성 그래프

```
[병렬 실행]                              [순차 실행]

┌──────────────────────┐
│ A: Storage Layer     │──┐
│ models.py, repo.py   │  │
├──────────────────────┤  │
│ B: Bot Infra         │  │
│ middleware.py(new)    │  │
│ config.py, format.py │  ├──→  F: Integration
├──────────────────────┤  │      handlers.py
│ C: Pipelines         │  │      main.py
│ pipelines/(new)      │──┤      scheduler.py
├──────────────────────┤  │
│ D: Orchestrator      │  │
│ orchestrator.py(new) │──┤
├──────────────────────┤  │
│ E: Writing Agent     │──┘
│ writing_agent.py(new)│
│ file_parser.py(new)  │
└──────────────────────┘
```

- **A~E**: 동시 실행 가능 (파일 소유권 겹침 없음)
- **F**: A~E 모두 완료 후 실행 (기존 파일 수정 + 전체 연결)

---

## 파일 소유권

| 에이전트 | 생성 (NEW) | 수정 (MODIFY) | 읽기만 (READ) |
|---------|-----------|--------------|--------------|
| **A: Storage** | — | `src/storage/models.py`, `src/storage/repository.py` | — |
| **B: Bot Infra** | `src/bot/middleware.py` | `src/config.py`, `src/bot/formatters.py` | — |
| **C: Pipelines** | `src/pipelines/__init__.py`, `check.py`, `report.py` | — | `src/bot/handlers.py` (복사용) |
| **D: Orchestrator** | `src/agents/orchestrator.py` | — | — |
| **E: Writing Agent** | `src/agents/writing_agent.py`, `src/tools/file_parser.py` | — | — |
| **F: Integration** | — | `src/bot/handlers.py`, `main.py`, `src/bot/scheduler.py` | 모든 신규 파일 |

**핵심 규칙**: 두 에이전트가 같은 파일을 동시에 수정하지 않는다.

---

## 인터페이스 계약

병렬 에이전트는 아직 존재하지 않는 다른 에이전트의 함수를 호출해야 한다.
아래 시그니처를 계약으로 확정하고, 각 에이전트는 이 계약에 맞춰 코딩한다.

### A: Storage Layer가 노출하는 함수
```python
# src/storage/repository.py (추가)
async def save_conversation(
    db, telegram_id: str, role: str, content: str,
    attachment_meta: dict | None, message_type: str,
) -> None

async def get_recent_conversations(
    db, telegram_id: str, days: int = 3, limit: int = 50,
) -> list[dict]
# 반환: [{id, role, content, attachment_meta(dict|None), message_type, created_at}, ...]

async def get_writing_style(
    db, journalist_id: int, department: str,
) -> dict
# 반환: {"rules": dict, "examples": list[str]}
```

### B: Bot Infra가 노출하는 함수
```python
# src/bot/middleware.py (신규)
async def conversation_logger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None
async def tracked_reply(original_reply_fn, db, telegram_id: str, text: str, **kwargs) -> Message

# src/bot/formatters.py (추가)
def format_writing_result(article: dict) -> list[str]
# article: {headline, body, sources, verified, verification_issues}
# 반환: 4096자 이하 메시지 리스트
```

### C: Pipelines가 노출하는 함수
```python
# src/pipelines/check.py (신규)
async def run_check(db, journalist: dict) -> tuple[list[dict] | None, datetime, datetime, int]
# 반환: (results, since, now, haiku_filtered_count)

# src/pipelines/report.py (신규)
async def run_report(db, journalist: dict, existing_items: list[dict] | None = None) -> list[dict] | None
```

### D: Orchestrator가 노출하는 함수
```python
# src/agents/orchestrator.py (신규)
async def pre_callback(db, api_key: str, telegram_id: str, current_query: str) -> dict
# 반환: {"relevant_messages": list[dict], "attachment_metas": list[dict]}

async def orchestrator_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None
```

### E: Writing Agent가 노출하는 함수
```python
# src/agents/writing_agent.py (신규)
async def run_writing_agent(api_key: str, context_data: dict, bot_context) -> dict
# 반환: {headline, body, word_count, sources, verified, verification_issues}

# src/tools/file_parser.py (신규)
async def extract_text(file_bytes: bytes, mime_type: str) -> str
```

---

## 실행 순서

```
Step 1: A~E 병렬 실행 (Task tool 5개 동시 호출)
Step 2: A~E 모두 완료 대기 (TaskOutput)
Step 3: F 통합 에이전트 실행
Step 4: 전체 테스트 (pytest)
```

---

## 공통 규칙

- **LLM 모델**: `claude-haiku-4-5-20251001` (모든 호출)
- **DB**: `aiosqlite`, `db.execute()` 패턴, `aiosqlite.Row` → dict 변환
- **주석**: 한글, 이모지 없음, 핵심 설명만
- **import**: `from src.xxx import yyy` 형태
- **에러 처리**: 기존 패턴 따름 (try/except, RuntimeError, 재시도)
- **테스트**: `pytest-asyncio` (asyncio_mode="auto"), 각 에이전트는 자기 코드의 테스트만 작성
