import json
import pytest
import pytest_asyncio

from src.storage.models import init_db
from src.storage import repository as repo


@pytest_asyncio.fixture
async def db(tmp_path):
    """테스트용 인메모리 대신 tmp 디렉토리에 DB 생성."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


# --- 스키마 ---

@pytest.mark.asyncio
async def test_tables_created(db):
    """4개 테이블이 모두 생성되어야 한다."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    rows = await cursor.fetchall()
    tables = [r["name"] for r in rows]
    assert "journalists" in tables
    assert "report_cache" in tables
    assert "report_items" in tables
    assert "reported_articles" in tables


# --- journalists CRUD ---

@pytest.mark.asyncio
async def test_upsert_and_get_journalist(db):
    """프로필 등록 후 조회하면 동일한 데이터가 반환된다."""
    jid = await repo.upsert_journalist(
        db,
        telegram_id="12345",
        name="김철수",
        department="사회부",
        keywords=["서부지검", "서부지법"],
        api_key="sk-ant-test-key",
    )
    assert jid > 0

    j = await repo.get_journalist(db, "12345")
    assert j["name"] == "김철수"
    assert j["department"] == "사회부"
    assert j["keywords"] == ["서부지검", "서부지법"]
    assert j["api_key"] == "sk-ant-test-key"  # 복호화된 값


@pytest.mark.asyncio
async def test_upsert_overwrites(db):
    """같은 telegram_id로 재등록하면 덮어쓴다."""
    await repo.upsert_journalist(db, "12345", "김철수", "사회부", ["A"], "key1")
    await repo.upsert_journalist(db, "12345", "박영희", "정치부", ["B"], "key2")

    j = await repo.get_journalist(db, "12345")
    assert j["name"] == "박영희"
    assert j["department"] == "정치부"
    assert j["api_key"] == "key2"


@pytest.mark.asyncio
async def test_get_journalist_not_found(db):
    """존재하지 않는 telegram_id 조회 시 None."""
    assert await repo.get_journalist(db, "nonexist") is None


# --- API 키 암호화 ---

def test_encrypt_decrypt_roundtrip():
    """암호화 → 복호화 왕복 검증."""
    original = "sk-ant-api03-very-long-key-12345"
    encrypted = repo.encrypt_api_key(original)
    assert encrypted != original
    assert repo.decrypt_api_key(encrypted) == original


@pytest.mark.asyncio
async def test_update_api_key(db):
    """API 키 변경 후 새 키로 조회된다."""
    await repo.upsert_journalist(db, "99", "테스트", "경제부", ["주식"], "old-key")
    await repo.update_api_key(db, "99", "new-key")

    j = await repo.get_journalist(db, "99")
    assert j["api_key"] == "new-key"


# --- last_check_at ---

@pytest.mark.asyncio
async def test_update_last_check_at(db):
    """last_check_at 갱신 후 값이 기록된다."""
    jid = await repo.upsert_journalist(db, "77", "테스트", "사회부", ["a"], "k")

    j = await repo.get_journalist(db, "77")
    assert j["last_check_at"] is None

    await repo.update_last_check_at(db, jid)

    j = await repo.get_journalist(db, "77")
    assert j["last_check_at"] is not None


# --- reported_articles ---

@pytest.mark.asyncio
async def test_save_and_get_reported_articles(db):
    """보고 이력 저장 후 조회된다."""
    jid = await repo.upsert_journalist(db, "55", "테스트", "사회부", ["a"], "k")

    articles = [
        {
            "topic_cluster": "서부지검 수사",
            "key_facts": ["대표 소환", "회계장부 압수"],
            "summary": "서부지검이 대표를 소환했다.",
            "article_urls": ["https://example.com/1"],
            "category": "important",
        },
        {
            "topic_cluster": "영등포서 사건",
            "key_facts": ["피의자 체포"],
            "summary": "영등포서가 피의자를 체포했다.",
            "article_urls": ["https://example.com/2"],
            "category": "exclusive",
        },
    ]
    await repo.save_reported_articles(db, jid, articles)

    result = await repo.get_recent_reported_articles(db, jid, hours=1)
    assert len(result) == 2
    topics = {r["topic_cluster"] for r in result}
    assert topics == {"서부지검 수사", "영등포서 사건"}


# --- today report_items (Phase 2 대비) ---

@pytest.mark.asyncio
async def test_get_today_report_items_empty(db):
    """report_items가 없으면 빈 리스트."""
    jid = await repo.upsert_journalist(db, "33", "테스트", "사회부", ["a"], "k")
    result = await repo.get_today_report_items(db, jid)
    assert result == []
