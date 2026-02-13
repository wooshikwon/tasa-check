import json
from datetime import UTC, datetime, timedelta, timezone

_KST = timezone(timedelta(hours=9))

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
    department: str,
    keywords: list[str],
    api_key: str,
) -> int:
    """프로필 등록 또는 갱신. journalist id를 반환한다."""
    encrypted_key = encrypt_api_key(api_key)
    keywords_json = json.dumps(keywords, ensure_ascii=False)
    await db.execute(
        """
        INSERT INTO journalists (telegram_id, department, keywords, api_key)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            department = excluded.department,
            keywords = excluded.keywords,
            api_key = excluded.api_key,
            last_check_at = NULL,
            last_report_at = NULL
        """,
        (telegram_id, department, keywords_json, encrypted_key),
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
        "department": row["department"],
        "keywords": json.loads(row["keywords"]),
        "api_key": decrypt_api_key(row["api_key"]),
        "last_check_at": row["last_check_at"],
        "last_report_at": row["last_report_at"] if "last_report_at" in row.keys() else None,
        "created_at": row["created_at"],
    }


async def clear_journalist_data(db: aiosqlite.Connection, journalist_id: int) -> None:
    """기자의 report/check 데이터를 삭제한다. 스케줄은 유지."""
    await db.execute(
        """
        DELETE FROM report_items WHERE report_cache_id IN (
            SELECT id FROM report_cache WHERE journalist_id = ?
        )
        """,
        (journalist_id,),
    )
    await db.execute("DELETE FROM report_cache WHERE journalist_id = ?", (journalist_id,))
    await db.execute("DELETE FROM reported_articles WHERE journalist_id = ?", (journalist_id,))
    await db.commit()


async def update_api_key(db: aiosqlite.Connection, telegram_id: str, api_key: str) -> None:
    """API 키만 변경한다."""
    encrypted_key = encrypt_api_key(api_key)
    await db.execute(
        "UPDATE journalists SET api_key = ? WHERE telegram_id = ?",
        (encrypted_key, telegram_id),
    )
    await db.commit()


async def update_keywords(db: aiosqlite.Connection, telegram_id: str, keywords: list[str]) -> None:
    """키워드를 변경하고 last_check_at/last_report_at을 초기화한다."""
    keywords_json = json.dumps(keywords, ensure_ascii=False)
    await db.execute(
        "UPDATE journalists SET keywords = ?, last_check_at = NULL, last_report_at = NULL WHERE telegram_id = ?",
        (keywords_json, telegram_id),
    )
    await db.commit()


async def clear_check_data(db: aiosqlite.Connection, journalist_id: int) -> None:
    """check 이력(reported_articles)만 삭제한다. report 이력은 유지."""
    await db.execute("DELETE FROM reported_articles WHERE journalist_id = ?", (journalist_id,))
    await db.commit()


async def update_department(db: aiosqlite.Connection, telegram_id: str, department: str) -> None:
    """부서를 변경하고 last_check_at/last_report_at을 초기화한다."""
    await db.execute(
        "UPDATE journalists SET department = ?, last_check_at = NULL, last_report_at = NULL WHERE telegram_id = ?",
        (department, telegram_id),
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


async def update_last_report_at(db: aiosqlite.Connection, journalist_id: int) -> None:
    """마지막 /report 시각을 현재로 갱신한다."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE journalists SET last_report_at = ? WHERE id = ?",
        (now, journalist_id),
    )
    await db.commit()


# --- reported_articles ---

async def save_reported_articles(
    db: aiosqlite.Connection,
    journalist_id: int,
    articles: list[dict],
) -> None:
    """Claude가 분석한 기사들을 저장한다 (skip 포함).

    articles 각 항목: {topic_cluster, key_facts, summary, url, category, reason}
    """
    now = datetime.now(UTC).isoformat()
    for article in articles:
        url = article.get("url", "")
        await db.execute(
            """
            INSERT INTO reported_articles
                (journalist_id, checked_at, topic_cluster, key_facts, summary, article_urls, category, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                journalist_id,
                now,
                article.get("topic_cluster", ""),
                json.dumps(article.get("key_facts", []), ensure_ascii=False),
                article.get("summary", ""),
                json.dumps([url] if url else [], ensure_ascii=False),
                article["category"],
                article.get("reason", ""),
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
            "reason": r["reason"] if "reason" in r.keys() else "",
        }
        for r in rows
    ]


# --- report_cache / report_items ---

async def get_or_create_report_cache(
    db: aiosqlite.Connection,
    journalist_id: int,
    date: str,
) -> tuple[int, bool]:
    """당일 report_cache를 조회하거나 생성한다.

    Returns:
        (cache_id, is_new) — is_new가 True면 시나리오 A, False면 시나리오 B.
    """
    cursor = await db.execute(
        "SELECT id FROM report_cache WHERE journalist_id = ? AND date = ?",
        (journalist_id, date),
    )
    row = await cursor.fetchone()
    if row:
        return row["id"], False

    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        "INSERT INTO report_cache (journalist_id, date, updated_at) VALUES (?, ?, ?)",
        (journalist_id, date, now),
    )
    await db.commit()
    return cursor.lastrowid, True


async def get_report_items_by_cache(
    db: aiosqlite.Connection,
    report_cache_id: int,
) -> list[dict]:
    """특정 report_cache의 전체 항목을 조회한다."""
    cursor = await db.execute(
        "SELECT * FROM report_items WHERE report_cache_id = ? ORDER BY created_at",
        (report_cache_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "summary": r["summary"],
            "category": r["category"],
            "prev_reference": r["prev_reference"],
            "reason": r["reason"] if "reason" in r.keys() else "",
            "exclusive": bool(r["exclusive"]) if "exclusive" in r.keys() else False,
            "publisher": r["publisher"] if "publisher" in r.keys() else "",
            "pub_time": r["pub_time"] if "pub_time" in r.keys() else "",
            "key_facts": json.loads(r["key_facts"]) if "key_facts" in r.keys() else [],
            "source_count": r["source_count"] if "source_count" in r.keys() else 1,
        }
        for r in rows
    ]


async def save_report_items(
    db: aiosqlite.Connection,
    report_cache_id: int,
    items: list[dict],
) -> None:
    """report_items에 항목들을 저장한다."""
    now = datetime.now(UTC).isoformat()
    for item in items:
        await db.execute(
            """
            INSERT INTO report_items
                (report_cache_id, title, url, summary, tags, category,
                 prev_reference, reason, exclusive, publisher, pub_time,
                 key_facts, source_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_cache_id,
                item["title"],
                item["url"],
                item["summary"],
                json.dumps(item.get("tags", []), ensure_ascii=False),
                item["category"],
                item.get("prev_reference"),
                item.get("reason", ""),
                int(item.get("exclusive", False)),
                item.get("publisher", ""),
                item.get("pub_time", ""),
                json.dumps(item.get("key_facts", []), ensure_ascii=False),
                item.get("source_count", 1),
                now,
                now,
            ),
        )
    await db.commit()


async def update_report_item(
    db: aiosqlite.Connection,
    item_id: int,
    summary: str,
    reason: str | None = None,
    exclusive: bool | None = None,
    key_facts: list[str] | None = None,
) -> None:
    """기존 report_item을 갱신한다. 시나리오 B [수정] 처리용.

    summary는 항상 갱신. reason/exclusive/key_facts는 값이 전달된 경우만 갱신.
    """
    now = datetime.now(UTC).isoformat()
    fields = ["summary = ?", "updated_at = ?"]
    params: list = [summary, now]
    if reason is not None:
        fields.append("reason = ?")
        params.append(reason)
    if exclusive is not None:
        fields.append("exclusive = ?")
        params.append(int(exclusive))
    if key_facts is not None:
        fields.append("key_facts = ?")
        params.append(json.dumps(key_facts, ensure_ascii=False))
    params.append(item_id)
    await db.execute(
        f"UPDATE report_items SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_recent_report_items(
    db: aiosqlite.Connection,
    journalist_id: int,
    days: int = 2,
) -> list[dict]:
    """최근 N일간 report_items를 조회한다. follow_up/new 판단용 이력."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    cursor = await db.execute(
        """
        SELECT ri.title, ri.summary, ri.category, ri.key_facts, ri.created_at
        FROM report_items ri
        JOIN report_cache rc ON ri.report_cache_id = rc.id
        WHERE rc.journalist_id = ? AND rc.date >= ?
        ORDER BY ri.created_at DESC
        """,
        (journalist_id, cutoff),
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": r["title"],
            "summary": r["summary"],
            "category": r["category"],
            "key_facts": json.loads(r["key_facts"]) if r["key_facts"] else [],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def get_today_report_items(
    db: aiosqlite.Connection,
    journalist_id: int,
) -> list[dict]:
    """당일 report_items를 조회한다. /check의 맥락 로드용."""
    today = datetime.now(_KST).strftime("%Y-%m-%d")
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
            "category": r["category"],
        }
        for r in rows
    ]


# --- schedules ---

async def save_schedules(
    db: aiosqlite.Connection,
    journalist_id: int,
    command: str,
    times_kst: list[str],
) -> None:
    """해당 command의 기존 스케줄을 교체한다 (삭제 후 새로 저장)."""
    await db.execute(
        "DELETE FROM schedules WHERE journalist_id = ? AND command = ?",
        (journalist_id, command),
    )
    for t in times_kst:
        await db.execute(
            "INSERT INTO schedules (journalist_id, command, time_kst) VALUES (?, ?, ?)",
            (journalist_id, command, t),
        )
    await db.commit()


async def get_schedules(
    db: aiosqlite.Connection,
    journalist_id: int,
) -> list[dict]:
    """사용자의 전체 스케줄을 조회한다."""
    cursor = await db.execute(
        "SELECT command, time_kst FROM schedules WHERE journalist_id = ? ORDER BY command, time_kst",
        (journalist_id,),
    )
    rows = await cursor.fetchall()
    return [{"command": r["command"], "time_kst": r["time_kst"]} for r in rows]


async def get_all_schedules(db: aiosqlite.Connection) -> list[dict]:
    """전체 사용자의 스케줄을 조회한다. 서버 시작 시 JobQueue 복원용."""
    cursor = await db.execute(
        """
        SELECT s.journalist_id, s.command, s.time_kst, j.telegram_id
        FROM schedules s
        JOIN journalists j ON s.journalist_id = j.id
        ORDER BY s.journalist_id, s.command, s.time_kst
        """,
    )
    rows = await cursor.fetchall()
    return [
        {
            "journalist_id": r["journalist_id"],
            "command": r["command"],
            "time_kst": r["time_kst"],
            "telegram_id": r["telegram_id"],
        }
        for r in rows
    ]


async def delete_all_schedules(
    db: aiosqlite.Connection,
    journalist_id: int,
) -> None:
    """사용자의 전체 스케줄을 삭제한다."""
    await db.execute("DELETE FROM schedules WHERE journalist_id = ?", (journalist_id,))
    await db.commit()


# --- 캐시 정리 ---

async def cleanup_old_data(db: aiosqlite.Connection) -> None:
    """보관 기간이 지난 report_items, reported_articles를 삭제한다."""
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


# --- 관리자 통계 ---

async def get_admin_stats(db: aiosqlite.Connection) -> dict:
    """관리자용 전체 통계를 조회한다."""
    # 전체 사용자 수
    cur = await db.execute("SELECT COUNT(*) FROM journalists")
    total_users = (await cur.fetchone())[0]

    # 부서별 사용자 수
    cur = await db.execute(
        "SELECT department, COUNT(*) as cnt FROM journalists GROUP BY department ORDER BY cnt DESC"
    )
    dept_stats = [(r["department"], r["cnt"]) for r in await cur.fetchall()]

    # 스케줄 등록 사용자 수
    cur = await db.execute("SELECT COUNT(DISTINCT journalist_id) FROM schedules")
    schedule_users = (await cur.fetchone())[0]

    # 스케줄 총 건수 (check/report 별)
    cur = await db.execute(
        "SELECT command, COUNT(*) as cnt FROM schedules GROUP BY command"
    )
    schedule_stats = {r["command"]: r["cnt"] for r in await cur.fetchall()}

    # 사용자별 상세
    cur = await db.execute(
        """
        SELECT j.telegram_id, j.department, j.keywords, j.last_check_at, j.created_at,
               (SELECT COUNT(*) FROM schedules s WHERE s.journalist_id = j.id) as schedule_count
        FROM journalists j ORDER BY j.created_at
        """
    )
    users = []
    for r in await cur.fetchall():
        users.append({
            "telegram_id": r["telegram_id"],
            "department": r["department"],
            "keywords": json.loads(r["keywords"]),
            "last_check_at": r["last_check_at"],
            "created_at": r["created_at"],
            "schedule_count": r["schedule_count"],
        })

    return {
        "total_users": total_users,
        "dept_stats": dept_stats,
        "schedule_users": schedule_users,
        "schedule_stats": schedule_stats,
        "users": users,
    }
