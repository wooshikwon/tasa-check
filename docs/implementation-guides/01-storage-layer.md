# A: Storage Layer 구현 가이드

> 담당: DB 스키마 확장 + CRUD 함수 + 예시 기사 로더

---

## 담당 파일

| 파일 | 작업 |
|------|------|
| `src/storage/models.py` | conversations, writing_styles 테이블 DDL 추가 |
| `src/storage/repository.py` | 대화 CRUD + 스타일 조회 + 예시 기사 로더 추가 |

## 금지 사항

- 기존 함수 변경 금지 (추가만 허용)
- 다른 파일 수정 금지 (config.py, handlers.py 등)
- 기존 테이블 스키마 변경 금지

---

## 노출 인터페이스 (다른 에이전트가 사용)

```python
async def save_conversation(
    db, telegram_id: str, role: str, content: str,
    attachment_meta: dict | None, message_type: str,
) -> None

async def get_recent_conversations(
    db, telegram_id: str, days: int = 3, limit: int = 50,
) -> list[dict]
# 반환: [{id, role, content, attachment_meta(dict|None), message_type, created_at}]

async def get_writing_style(
    db, journalist_id: int, department: str,
) -> dict
# 반환: {"rules": dict, "examples": list[str]}
```

---

## 상세 구현

### 1. models.py — DDL 추가

기존 `init_db()` 함수 내부, `CREATE TABLE IF NOT EXISTS schedules` 이후에 추가한다.
기존 `executescript()` 블록의 SQL 문자열 끝에 이어서 추가하면 된다.

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

CREATE TABLE IF NOT EXISTS writing_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    publisher TEXT NOT NULL DEFAULT '',   -- 타겟 언론사 (빈 값 = 부서 기본)
    style_guide TEXT NOT NULL,            -- JSON: 작성 가이드
    example_articles TEXT DEFAULT '[]',   -- JSON: 예시 기사 배열 (향후 확장용)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(journalist_id, publisher)
);
```

### 2. repository.py — 신규 함수 추가

기존 import에 `json`, `Path` 추가 필요:
```python
import json
from pathlib import Path
```

#### save_conversation

```python
async def save_conversation(
    db,
    telegram_id: str,
    role: str,
    content: str,
    attachment_meta: dict | None,
    message_type: str,
) -> None:
    """대화 메시지를 conversations 테이블에 저장한다."""
    journalist = await get_journalist(db, telegram_id)
    if not journalist:
        return  # 미등록 사용자는 저장하지 않음
    await db.execute(
        """INSERT INTO conversations (journalist_id, role, content, attachment_meta, message_type)
           VALUES (?, ?, ?, ?, ?)""",
        (
            journalist["id"],
            role,
            content,
            json.dumps(attachment_meta, ensure_ascii=False) if attachment_meta else None,
            message_type,
        ),
    )
    await db.commit()
```

**기존 패턴 참고**: `save_reported_articles`의 INSERT + commit 패턴과 동일.

#### get_recent_conversations

```python
async def get_recent_conversations(
    db,
    telegram_id: str,
    days: int = 3,
    limit: int = 50,
) -> list[dict]:
    """최근 N일 내 대화를 조회한다. 최신순 정렬."""
    journalist = await get_journalist(db, telegram_id)
    if not journalist:
        return []
    cursor = await db.execute(
        """SELECT id, role, content, attachment_meta, message_type, created_at
           FROM conversations
           WHERE journalist_id = ?
             AND created_at >= datetime('now', ?)
           ORDER BY created_at DESC
           LIMIT ?""",
        (journalist["id"], f"-{days} days", limit),
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item["attachment_meta"]:
            item["attachment_meta"] = json.loads(item["attachment_meta"])
        result.append(item)
    return result
```

**주의**: `datetime('now', ...)` SQLite 함수는 UTC 기준. created_at도 UTC로 저장되므로 일관성 유지.

#### get_writing_style

```python
async def get_writing_style(
    db,
    journalist_id: int,
    department: str,
) -> dict:
    """스타일 규칙 + 예시 기사를 반환한다.

    DB에 사용자별 스타일이 있으면 사용, 없으면 config.py 부서 기본 가이드 반환.
    예시 기사는 articles/chosun/{department}/ 디렉토리에서 로드.
    """
    from src.config import WRITING_STYLES, WRITING_STYLES_DEFAULT

    cursor = await db.execute(
        "SELECT style_guide FROM writing_styles WHERE journalist_id = ? LIMIT 1",
        (journalist_id,),
    )
    row = await cursor.fetchone()
    rules = json.loads(row["style_guide"]) if row else WRITING_STYLES.get(department, WRITING_STYLES_DEFAULT)

    examples = _load_example_articles(department)
    return {"rules": rules, "examples": examples}
```

#### _load_example_articles (비공개 헬퍼)

```python
def _load_example_articles(department: str, max_count: int = 5) -> list[str]:
    """articles/chosun/{department}/ 디렉토리에서 예시 기사를 로드한다."""
    # 부서명 → 디렉토리명 매핑
    _DEPT_DIR_MAP = {
        "경제부": "economy",
        "산업부": "industry",
        "국제부": "international",
        "정치부": "politics",
        "사회부": "social",
        "테크부": "tech",
    }
    dir_name = _DEPT_DIR_MAP.get(department)
    if not dir_name:
        return []
    examples_dir = Path(__file__).parent.parent.parent / "articles" / "chosun" / dir_name
    if not examples_dir.exists():
        return []
    articles = []
    for md_file in sorted(examples_dir.glob("*.md"))[:max_count]:
        articles.append(md_file.read_text(encoding="utf-8"))
    return articles
```

**경로 설명**: `repository.py` → `src/storage/` → `parent.parent.parent` = 프로젝트 루트 → `articles/chosun/`

**부서-디렉토리 매핑**: config.py의 DEPARTMENT_PROFILES 키(한글)와 articles/ 디렉토리명(영문)이 다르므로 매핑 필요.

#### cleanup_old_data 확장

기존 `cleanup_old_data` 함수의 마지막에 conversations 삭제 로직 추가:

```python
# 기존 cleanup_old_data 함수 끝에 추가
await db.execute(
    "DELETE FROM conversations WHERE created_at < ?",
    (cutoff.isoformat(),),
)
```

**cutoff 변수**: 기존 함수에서 이미 `cutoff = datetime.now() - timedelta(days=CACHE_RETENTION_DAYS)` 형태로 계산됨. 이를 재사용.

---

## 테스트

`tests/test_storage_v2.py` 생성:

```python
import pytest
import aiosqlite
from src.storage.models import init_db
from src.storage.repository import (
    save_conversation,
    get_recent_conversations,
    get_writing_style,
    upsert_journalist,
)


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


async def test_save_and_get_conversations(db):
    """대화 저장 + 조회 왕복 테스트."""
    await upsert_journalist(db, "123", "경제부", ["테스트"], "test-key")
    await save_conversation(db, "123", "user", "안녕하세요", None, "text")
    await save_conversation(db, "123", "assistant", "반갑습니다", None, "text")

    convos = await get_recent_conversations(db, "123", days=1, limit=10)
    assert len(convos) == 2
    assert convos[0]["role"] == "assistant"  # 최신순
    assert convos[1]["role"] == "user"


async def test_conversation_with_attachment(db):
    """첨부파일 메타데이터 저장/조회."""
    await upsert_journalist(db, "123", "경제부", ["테스트"], "test-key")
    meta = {"file_id": "abc", "file_name": "doc.pdf", "mime_type": "application/pdf", "file_size": 1024}
    await save_conversation(db, "123", "user", "기사 써줘", meta, "document")

    convos = await get_recent_conversations(db, "123")
    assert convos[0]["attachment_meta"]["file_id"] == "abc"


async def test_get_writing_style_fallback(db):
    """DB에 레코드 없을 때 config.py 기본 가이드 반환."""
    await upsert_journalist(db, "123", "경제부", ["테스트"], "test-key")
    journalist_id = 1  # upsert 후 첫 번째 ID
    style = await get_writing_style(db, journalist_id, "경제부")
    assert "rules" in style
    assert "examples" in style
    assert isinstance(style["examples"], list)


async def test_unregistered_user_no_save(db):
    """미등록 사용자 대화는 저장하지 않음."""
    await save_conversation(db, "unknown", "user", "test", None, "text")
    convos = await get_recent_conversations(db, "unknown")
    assert convos == []
```

---

## 완료 기준

1. `init_db()` 실행 시 conversations, writing_styles 테이블이 정상 생성됨
2. `save_conversation` → `get_recent_conversations` 왕복 정상 동작
3. 첨부파일 meta JSON 저장/파싱 정상
4. `get_writing_style`이 config.py fallback 정상 반환
5. `_load_example_articles`가 articles/chosun/ 디렉토리에서 md 파일 로드
6. `cleanup_old_data`가 conversations도 정리
7. 기존 함수 (get_journalist, upsert_journalist 등) 동작에 영향 없음
