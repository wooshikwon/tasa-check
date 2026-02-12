# /schedule 자동 실행 기능 구현 계획

## 목적

사용자가 `/check`와 `/report`를 매번 수동 입력하지 않아도, 지정한 시각에 자동으로 실행되어 결과를 전송받을 수 있도록 한다.

## 사용자 경험

### 명령어

```
/schedule check 09:00 12:00 15:00    → 매일 9시, 12시, 15시에 자동 타사 체크
/schedule report 08:30               → 매일 08:30에 자동 부서 브리핑
/schedule off                        → 모든 자동 실행 해제
/schedule                            → 현재 설정 확인
```

- 시각은 KST 기준, `HH:MM` 형식
- check와 report를 각각 독립적으로 설정 가능
- 최대 시각 수 제한: check 5개, report 3개 (API 비용 고려)

### /start 완료 후 안내 메시지 개선

현재:
```
설정 완료!
부서: 사회부
키워드: 서부지검, 서부지법
/check - 타사 체크 | /report - 부서 주요 뉴스
```

변경:
```
설정 완료!
부서: 사회부
키워드: 서부지검, 서부지법

사용 가능한 명령어:
/check - 키워드 기반 타사 체크
/report - 부서 주요 뉴스 브리핑
/schedule - 자동 실행 예약 (예: /schedule check 09:00 12:00)
/schedule off - 자동 실행 예약 일괄 삭제
/setkey - Claude API 키 변경
```

## 기술 설계

### 1. DB 스키마 — `schedules` 테이블 추가

```sql
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    command TEXT NOT NULL,        -- "check" / "report"
    time_kst TEXT NOT NULL,       -- "HH:MM" 형식
    UNIQUE(journalist_id, command, time_kst)
);
```

- 서버 재시작 시 DB에서 스케줄을 복원하여 JobQueue에 재등록
- `/start`로 재등록 시 기존 스케줄도 초기화 (`clear_journalist_data`에 포함)

### 2. 스케줄 저장/조회 — `repository.py`

```python
async def save_schedules(db, journalist_id, command, times_kst: list[str]) -> None:
    """기존 스케줄을 교체한다 (해당 command의 기존 항목 삭제 후 새로 저장)."""

async def get_schedules(db, journalist_id) -> list[dict]:
    """사용자의 전체 스케줄을 조회한다."""

async def get_all_schedules(db) -> list[dict]:
    """전체 사용자의 스케줄을 조회한다. 서버 시작 시 JobQueue 복원용."""

async def delete_all_schedules(db, journalist_id) -> None:
    """사용자의 전체 스케줄을 삭제한다."""
```

### 3. 스케줄 핸들러 — `handlers.py`

```python
async def schedule_handler(update, context) -> None:
    """/schedule 명령 처리."""
```

분기:
- 인자 없음 → 현재 설정 표시
- `off` → 전체 해제
- `check HH:MM ...` 또는 `report HH:MM ...` → 저장 + JobQueue 등록

### 4. 자동 실행 콜백 — `scheduler.py` (신규 파일)

```python
async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue에서 호출되는 자동 check 실행."""

async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue에서 호출되는 자동 report 실행."""

async def restore_schedules(app: Application, db) -> None:
    """서버 시작 시 DB의 스케줄을 JobQueue에 복원."""
```

- `_run_check_pipeline`과 report 파이프라인을 재사용
- 결과를 `bot.send_message(chat_id=telegram_id, ...)` 로 직접 전송
- 기존 `_user_locks` 잠금을 동일하게 적용 (수동/자동 동시 실행 방지)
- 에러 발생 시 사용자에게 에러 메시지 전송 + 로깅

### 5. 진입점 — `main.py`

- `schedule_handler` 등록: `CommandHandler("schedule", schedule_handler)`
- `post_init`에서 `restore_schedules(app, db)` 호출

### 6. JobQueue 등록/해제 패턴

```python
# KST 시각을 UTC로 변환하여 등록
from datetime import time, timezone, timedelta
kst = timezone(timedelta(hours=9))
job_time = time(hour=9, minute=0, tzinfo=kst)

# 등록
app.job_queue.run_daily(
    scheduled_check,
    time=job_time,
    chat_id=telegram_id,
    name=f"check_{journalist_id}_{09:00}",
    data={"journalist_id": journalist_id},
)

# 해제 (사용자의 기존 job 제거)
for job in app.job_queue.get_jobs_by_name(f"check_{journalist_id}_*"):
    job.schedule_removal()
```

## 수정 대상 파일

| 파일 | 변경 내용 |
|---|---|
| `src/storage/models.py` | `schedules` 테이블 DDL 추가 |
| `src/storage/repository.py` | 스케줄 CRUD 함수 추가 |
| `src/bot/handlers.py` | `schedule_handler` 추가 |
| `src/bot/scheduler.py` | 신규 — 자동 실행 콜백 + 복원 로직 |
| `src/bot/conversation.py` | /start 완료 안내 메시지 개선 |
| `main.py` | schedule_handler 등록 + post_init에서 복원 |

## 구현 순서

1. DB 스키마 + repository 함수
2. `scheduler.py` — 자동 실행 콜백 (기존 파이프라인 재사용)
3. `schedule_handler` — 명령어 파싱 + DB 저장 + JobQueue 등록
4. `main.py` — 핸들러 등록 + 시작 시 복원
5. `conversation.py` — /start 안내 메시지 개선
6. 서버 배포 + 테스트
