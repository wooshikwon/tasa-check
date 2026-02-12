import aiosqlite

DDL = """
CREATE TABLE IF NOT EXISTS journalists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL,          -- JSON 배열
    api_key TEXT NOT NULL,           -- Fernet 암호화된 값
    last_check_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    date DATE NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(journalist_id, date)
);

CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_cache_id INTEGER NOT NULL REFERENCES report_cache(id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,              -- JSON 배열
    category TEXT NOT NULL,          -- "follow_up" / "new"
    prev_reference TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reported_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    checked_at DATETIME NOT NULL,
    topic_cluster TEXT NOT NULL,
    key_facts TEXT NOT NULL,         -- JSON 배열
    summary TEXT NOT NULL,
    article_urls TEXT NOT NULL,      -- JSON 배열
    category TEXT NOT NULL,          -- "exclusive" / "important" / "skip"
    reason TEXT DEFAULT ''           -- 판단 근거 (skip이면 스킵 사유)
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    command TEXT NOT NULL,           -- "check" / "report"
    time_kst TEXT NOT NULL,          -- "HH:MM" 형식
    UNIQUE(journalist_id, command, time_kst)
);
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    """DB 연결을 열고 스키마를 초기화한다."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(DDL)
    # 기존 DB 마이그레이션: reason 컬럼 추가
    try:
        await db.execute("ALTER TABLE reported_articles ADD COLUMN reason TEXT DEFAULT ''")
    except Exception:
        pass  # 이미 존재하면 무시
    await db.commit()
    return db
