# F: Integration 구현 가이드

> 담당: 모든 병렬 에이전트 결과물을 연결하는 최종 통합
> **실행 조건**: Agent A~E 모두 완료 후 실행

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/bot/handlers.py` | 수정 — 파이프라인 함수 제거 + import 변경 |
| `main.py` | 수정 — 미들웨어 등록 + MessageHandler 등록 |
| `src/bot/scheduler.py` | 수정 — import 경로 변경 (pipelines/) |
| `pyproject.toml` | 수정 — 의존성 추가 (pymupdf, python-docx) |

## 금지 사항

- 병렬 에이전트가 생성한 신규 파일 수정 금지 (orchestrator.py, writing_agent.py, middleware.py, file_parser.py, pipelines/)
- 기존 동작 변경 없이 통합해야 함 (/check, /report 등 기존 커맨드 동일 동작)

---

## 전제 조건

아래 파일이 모두 존재해야 한다:
- `src/storage/models.py` — conversations, writing_styles DDL 추가 완료
- `src/storage/repository.py` — save_conversation, get_recent_conversations, get_writing_style 추가 완료
- `src/bot/middleware.py` — conversation_logger, tracked_reply 구현 완료
- `src/config.py` — WRITING_STYLES 추가 완료
- `src/bot/formatters.py` — format_writing_result 추가 완료
- `src/pipelines/__init__.py`, `check.py`, `report.py` — 파이프라인 분리 완료
- `src/agents/orchestrator.py` — Router + Pre-callback + 핸들러 구현 완료
- `src/agents/writing_agent.py` — 에이전트 루프 + verification 구현 완료
- `src/tools/file_parser.py` — 파일 파서 구현 완료

---

## 상세 구현

### 1. handlers.py 수정

#### 제거 대상

다음 함수/상수를 handlers.py에서 **삭제**한다 (pipelines/로 이동 완료):

```python
# 삭제할 함수들:
_SKIP_TITLE_TAGS = {...}       # → pipelines/__init__.py
_normalize_title()              # → pipelines/__init__.py::normalize_title
_match_article()                # → pipelines/__init__.py::match_article
_map_results_to_articles()      # → pipelines/__init__.py::map_results_to_articles
_run_check_pipeline()           # → pipelines/check.py::run_check
_run_report_pipeline()          # → pipelines/report.py::run_report
```

#### import 변경

```python
# 추가
from src.pipelines.check import run_check
from src.pipelines.report import run_report
```

#### check_handler 수정

기존:
```python
async def check_handler(update, context):
    ...
    async with _pipeline_semaphore:
        results, since, now, haiku_count = await _run_check_pipeline(db, journalist)
    ...
```

변경:
```python
async def check_handler(update, context):
    ...
    async with _pipeline_semaphore:
        results, since, now, haiku_count = await run_check(db, journalist)
    ...
```

#### report_handler 수정

동일 패턴으로 `_run_report_pipeline` → `run_report` 변경.

#### 유지 대상

다음은 handlers.py에 그대로 남긴다:
- `_user_locks` (사용자별 동시 실행 방지)
- `_pipeline_semaphore` (전역 파이프라인 동시 제한)
- `check_handler`, `report_handler` (함수 자체는 유지, 내부 호출만 변경)
- `set_division_handler`, `set_division_callback`
- `status_handler`, `stats_handler`
- `format_error_message`
- `_handle_report_scenario_a`, `_handle_report_scenario_b` (report_handler 내부 시나리오 처리)

### 2. scheduler.py 수정

#### import 변경

기존:
```python
# scheduler.py 내부에서 handlers의 파이프라인 함수를 사용
# (직접 import이든 간접 호출이든)
```

변경:
```python
from src.pipelines.check import run_check
from src.pipelines.report import run_report
```

scheduled_check, scheduled_report 내부에서 `_run_check_pipeline` → `run_check`, `_run_report_pipeline` → `run_report`로 변경.

**주의**: `_user_locks`와 `_pipeline_semaphore`는 여전히 handlers.py에서 import. 이 부분은 유지.

### 3. main.py 수정

#### 미들웨어 등록

`post_init` 함수 또는 handler 등록 부분에 추가:

```python
from telegram.ext import MessageHandler, filters
from src.bot.middleware import conversation_logger
from src.agents.orchestrator import orchestrator_handler
```

handler 등록 부분:
```python
# 기존 핸들러 등록 (group=0, 기본값)
app.add_handler(build_conversation_handler())        # /start
app.add_handler(build_settings_handler())            # /set_*
app.add_handler(CommandHandler("check", check_handler))
app.add_handler(CommandHandler("report", report_handler))
app.add_handler(CommandHandler("set_division", set_division_handler))
app.add_handler(CallbackQueryHandler(set_division_callback))
app.add_handler(CommandHandler("status", status_handler))
app.add_handler(CommandHandler("stats", stats_handler))

# 미들웨어: 모든 메시지 로깅 (group=-1, 최우선)
app.add_handler(
    MessageHandler(filters.ALL, conversation_logger),
    group=-1,
)

# Orchestrator: 자연어 메시지 처리 (group=1, ConversationHandler 이후)
app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, orchestrator_handler),
    group=1,
)
app.add_handler(
    MessageHandler(filters.Document.ALL | filters.PHOTO, orchestrator_handler),
    group=1,
)
```

**group 우선순위 설명**:
- `group=-1`: 미들웨어 (항상 먼저, 모든 메시지 로깅)
- `group=0`: 기존 핸들러 (ConversationHandler, CommandHandler). ConversationHandler가 상태 중이면 텍스트를 소비
- `group=1`: Orchestrator (ConversationHandler가 매칭하지 않은 텍스트만 도달)

**충돌 방지**: ConversationHandler(group=0)가 /set_keyword, /set_apikey 등의 입력 대기 상태에서 텍스트를 소비하므로, orchestrator(group=1)는 해당 텍스트를 받지 않는다.

### 4. pyproject.toml 수정

```toml
dependencies = [
    # 기존 의존성
    "python-telegram-bot[job-queue]>=21.0",
    "anthropic>=0.40.0",
    "httpx>=0.27.0",
    "beautifulsoup4>=4.12.0",
    "aiosqlite>=0.20.0",
    "cryptography>=43.0.0",
    "python-dotenv>=1.0.0",
    "langfuse>=3.14.1",
    "opentelemetry-instrumentation-anthropic>=0.52.3",
    # v2 신규
    "pymupdf>=1.24.0",           # PDF 텍스트 추출
    "python-docx>=1.1.0",        # DOCX 텍스트 추출
]
```

수정 후 `uv sync` 실행하여 의존성 설치.

---

## 검증 체크리스트

통합 후 다음을 모두 확인:

### 기존 기능 회귀 테스트
- [ ] `/start` → 등록 프로세스 정상
- [ ] `/check` → 타사 체크 정상 (기존과 동일한 결과)
- [ ] `/report` → 브리핑 정상 (기존과 동일한 결과)
- [ ] `/set_keyword` → 키워드 변경 정상
- [ ] `/set_apikey` → API 키 설정 정상
- [ ] `/set_schedule` → 스케줄 설정 정상
- [ ] `/set_division` → 부서 변경 정상
- [ ] `/status`, `/stats` → 정상

### 신규 기능 테스트
- [ ] 자연어 "타사 체크 해줘" → Orchestrator → check 파이프라인
- [ ] 자연어 "브리핑 줘" → Orchestrator → report 파이프라인
- [ ] 자연어 "기사 써줘" → Orchestrator → Writing Agent
- [ ] 자연어 "제목 바꿔줘" → Orchestrator → edit_article
- [ ] 자연어 "고마워" → Orchestrator → conversation
- [ ] 자연어 "날씨 알려줘" → Orchestrator → reject
- [ ] 파일만 전송 → "파일을 받았습니다" 안내
- [ ] 파일 + 캡션 "기사 써줘" → Writing Agent
- [ ] /set_keyword 입력 대기 중 텍스트 → ConversationHandler가 소비 (Orchestrator 미도달)

### 미들웨어 확인
- [ ] 모든 메시지가 conversations 테이블에 저장됨
- [ ] /command, 텍스트, 파일 첨부 모두 정상 기록

---

## 테스트

`tests/test_integration.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def test_existing_check_command_works():
    """기존 /check 커맨드가 pipeline 분리 후에도 동작하는지 확인."""
    # check_handler가 run_check를 정상 호출하는지 mock 테스트
    ...


async def test_orchestrator_registered():
    """MessageHandler(group=1)로 orchestrator가 등록되었는지 확인."""
    # main.py의 핸들러 등록 검증
    ...


async def test_middleware_registered():
    """MessageHandler(group=-1)로 conversation_logger가 등록되었는지 확인."""
    ...
```

---

## 완료 기준

1. handlers.py에서 파이프라인 함수 5개 + 상수 제거, pipelines/ import로 대체
2. scheduler.py의 파이프라인 호출도 pipelines/ import로 대체
3. main.py에 미들웨어(group=-1) + Orchestrator(group=1) 등록
4. pyproject.toml에 pymupdf, python-docx 추가 + `uv sync`
5. 기존 /command 핸들러 전부 정상 동작 (회귀 없음)
6. ConversationHandler(group=0)와 Orchestrator(group=1) 충돌 없음
7. 모든 검증 체크리스트 통과
