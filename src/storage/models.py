import aiosqlite

DDL = """
CREATE TABLE IF NOT EXISTS journalists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL,          -- JSON 배열
    api_key TEXT NOT NULL,           -- Fernet 암호화된 값
    last_check_at DATETIME,
    last_report_at DATETIME,
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
    reason TEXT DEFAULT '',          -- 선택 사유
    exclusive INTEGER DEFAULT 0,    -- [단독] 여부 (0/1)
    publisher TEXT DEFAULT '',      -- 언론사명
    pub_time TEXT DEFAULT '',       -- 배포 시각 (HH:MM)
    key_facts TEXT DEFAULT '[]',   -- 핵심 팩트 JSON 배열
    source_count INTEGER DEFAULT 1, -- 통합 출처 수
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
    # 기존 DB 마이그레이션
    _migrations = [
        "ALTER TABLE reported_articles ADD COLUMN reason TEXT DEFAULT ''",
        "ALTER TABLE report_items ADD COLUMN reason TEXT DEFAULT ''",
        "ALTER TABLE report_items ADD COLUMN exclusive INTEGER DEFAULT 0",
        "ALTER TABLE report_items ADD COLUMN publisher TEXT DEFAULT ''",
        "ALTER TABLE report_items ADD COLUMN pub_time TEXT DEFAULT ''",
        "ALTER TABLE report_items ADD COLUMN key_facts TEXT DEFAULT '[]'",
        "ALTER TABLE journalists ADD COLUMN last_report_at DATETIME",
        "ALTER TABLE report_items ADD COLUMN source_count INTEGER DEFAULT 1",
    ]
    for sql in _migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass  # 이미 존재하면 무시
    await db.commit()
    return db
