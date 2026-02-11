import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from cryptography.fernet import Fernet

from src.config import FERNET_KEY, CACHE_RETENTION_DAYS

_fernet = Fernet(FERNET_KEY.encode())


def encrypt_api_key(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()


# --- journalists ---

async def upsert_journalist(
    db: aiosqlite.Connection,
    telegram_id: str,
    name: str,
    department: str,
    keywords: list[str],
    api_key: str,
) -> int:
    """프로필 등록 또는 갱신. journalist id를 반환한다."""
    encrypted_key = encrypt_api_key(api_key)
    keywords_json = json.dumps(keywords, ensure_ascii=False)
    await db.execute(
        """
        INSERT INTO journalists (telegram_id, name, department, keywords, api_key)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            name = excluded.name,
            department = excluded.department,
            keywords = excluded.keywords,
            api_key = excluded.api_key
        """,
        (telegram_id, name, department, keywords_json, encrypted_key),
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT id FROM journalists WHERE telegram_id = ?", (telegram_id,)
    )
    row = await cursor.fetchone()
    return row["id"]


async def get_journalist(db: aiosqlite.Connection, telegram_id: str) -> dict | None:
    """telegram_id로 기자 프로필 조회. 없으면 None."""
    cursor = await db.execute(
        "SELECT * FROM journalists WHERE telegram_id = ?", (telegram_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "name": row["name"],
        "department": row["department"],
        "keywords": json.loads(row["keywords"]),
        "api_key": decrypt_api_key(row["api_key"]),
        "last_check_at": row["last_check_at"],
        "created_at": row["created_at"],
    }


async def update_api_key(db: aiosqlite.Connection, telegram_id: str, api_key: str) -> None:
    """API 키만 변경한다."""
    encrypted_key = encrypt_api_key(api_key)
    await db.execute(
        "UPDATE journalists SET api_key = ? WHERE telegram_id = ?",
        (encrypted_key, telegram_id),
    )
    await db.commit()


async def update_last_check_at(db: aiosqlite.Connection, journalist_id: int) -> None:
    """마지막 /check 시각을 현재로 갱신한다."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE journalists SET last_check_at = ? WHERE id = ?",
        (now, journalist_id),
    )
    await db.commit()


# --- reported_articles ---

async def save_reported_articles(
    db: aiosqlite.Connection,
    journalist_id: int,
    articles: list[dict],
) -> None:
    """Claude가 분석한 보고 기사들을 저장한다.

    articles 각 항목: {topic_cluster, key_facts, summary, article_urls, category}
    """
    now = datetime.now(UTC).isoformat()
    for article in articles:
        await db.execute(
            """
            INSERT INTO reported_articles
                (journalist_id, checked_at, topic_cluster, key_facts, summary, article_urls, category)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                journalist_id,
                now,
                article["topic_cluster"],
                json.dumps(article["key_facts"], ensure_ascii=False),
                article["summary"],
                json.dumps(article["article_urls"], ensure_ascii=False),
                article["category"],
            ),
        )
    await db.commit()


async def get_recent_reported_articles(
    db: aiosqlite.Connection,
    journalist_id: int,
    hours: int = 24,
) -> list[dict]:
    """최근 N시간 이내 보고 이력을 조회한다."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    cursor = await db.execute(
        """
        SELECT * FROM reported_articles
        WHERE journalist_id = ? AND checked_at >= ?
        ORDER BY checked_at DESC
        """,
        (journalist_id, since),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r["id"],
            "checked_at": r["checked_at"],
            "topic_cluster": r["topic_cluster"],
            "key_facts": json.loads(r["key_facts"]),
            "summary": r["summary"],
            "article_urls": json.loads(r["article_urls"]),
            "category": r["category"],
        }
        for r in rows
    ]


# --- report_items (Phase 2 대비, /check에서 맥락 로드용) ---

async def get_today_report_items(
    db: aiosqlite.Connection,
    journalist_id: int,
) -> list[dict]:
    """당일 report_items를 조회한다. /check의 맥락 로드용."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    cursor = await db.execute(
        """
        SELECT ri.* FROM report_items ri
        JOIN report_cache rc ON ri.report_cache_id = rc.id
        WHERE rc.journalist_id = ? AND rc.date = ?
        ORDER BY ri.created_at
        """,
        (journalist_id, today),
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": r["title"],
            "url": r["url"],
            "summary": r["summary"],
            "tags": json.loads(r["tags"]),
            "category": r["category"],
        }
        for r in rows
    ]


# --- 캐시 정리 ---

async def cleanup_old_data(db: aiosqlite.Connection) -> None:
    """14일 이상 지난 report_items, reported_articles를 삭제한다."""
    cutoff = (datetime.now(UTC) - timedelta(days=CACHE_RETENTION_DAYS)).isoformat()

    # report_items: report_cache 기준으로 삭제
    await db.execute(
        """
        DELETE FROM report_items WHERE report_cache_id IN (
            SELECT id FROM report_cache WHERE date < ?
        )
        """,
        (cutoff[:10],),  # DATE 형식 비교
    )
    await db.execute("DELETE FROM report_cache WHERE date < ?", (cutoff[:10],))
    await db.execute(
        "DELETE FROM reported_articles WHERE checked_at < ?", (cutoff,)
    )
    await db.commit()
