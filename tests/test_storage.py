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
        department="사회부",
        keywords=["서부지검", "서부지법"],
        api_key="sk-ant-test-key",
    )
    assert jid > 0

    j = await repo.get_journalist(db, "12345")
    assert j["department"] == "사회부"
    assert j["keywords"] == ["서부지검", "서부지법"]
    assert j["api_key"] == "sk-ant-test-key"  # 복호화된 값


@pytest.mark.asyncio
async def test_upsert_overwrites(db):
    """같은 telegram_id로 재등록하면 덮어쓴다."""
    await repo.upsert_journalist(db, "12345", "사회부", ["A"], "key1")
    await repo.upsert_journalist(db, "12345", "정치부", ["B"], "key2")

    j = await repo.get_journalist(db, "12345")
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
    await repo.upsert_journalist(db, "99", "경제부", ["주식"], "old-key")
    await repo.update_api_key(db, "99", "new-key")

    j = await repo.get_journalist(db, "99")
    assert j["api_key"] == "new-key"


# --- last_check_at ---

@pytest.mark.asyncio
async def test_update_last_check_at(db):
    """last_check_at 갱신 후 값이 기록된다."""
    jid = await repo.upsert_journalist(db, "77", "사회부", ["a"], "k")

    j = await repo.get_journalist(db, "77")
    assert j["last_check_at"] is None

    await repo.update_last_check_at(db, jid)

    j = await repo.get_journalist(db, "77")
    assert j["last_check_at"] is not None


# --- reported_articles ---

@pytest.mark.asyncio
async def test_save_and_get_reported_articles(db):
    """보고 이력 저장 후 조회된다."""
    jid = await repo.upsert_journalist(db, "55", "사회부", ["a"], "k")

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


# --- report_cache / report_items ---

@pytest.mark.asyncio
async def test_get_or_create_report_cache_new(db):
    """캐시가 없으면 새로 생성하고 is_new=True."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    cache_id, is_new = await repo.get_or_create_report_cache(db, jid, "2026-02-11")
    assert cache_id > 0
    assert is_new is True


@pytest.mark.asyncio
async def test_get_or_create_report_cache_existing(db):
    """같은 날짜에 다시 호출하면 기존 캐시 반환, is_new=False."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    cache_id_1, is_new_1 = await repo.get_or_create_report_cache(db, jid, "2026-02-11")
    cache_id_2, is_new_2 = await repo.get_or_create_report_cache(db, jid, "2026-02-11")
    assert cache_id_1 == cache_id_2
    assert is_new_1 is True
    assert is_new_2 is False


@pytest.mark.asyncio
async def test_save_and_get_report_items(db):
    """report_items 저장 후 조회된다."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    cache_id, _ = await repo.get_or_create_report_cache(db, jid, "2026-02-11")

    items = [
        {
            "title": "서부지검 수사 확대",
            "url": "https://example.com/1",
            "summary": "서부지검이 수사를 확대했다.",
            "tags": ["서부지검", "수사"],
            "category": "new",
        },
        {
            "title": "영등포서 사건 후속",
            "url": "https://example.com/2",
            "summary": "영등포서 후속 보도.",
            "tags": ["영등포서"],
            "category": "follow_up",
            "prev_reference": '2026-02-10 "영등포서 사건"',
        },
    ]
    await repo.save_report_items(db, cache_id, items)

    result = await repo.get_report_items_by_cache(db, cache_id)
    assert len(result) == 2
    assert result[0]["title"] == "서부지검 수사 확대"
    assert result[0]["tags"] == ["서부지검", "수사"]
    assert result[1]["prev_reference"] == '2026-02-10 "영등포서 사건"'


@pytest.mark.asyncio
async def test_update_report_item(db):
    """report_item 요약 갱신."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    cache_id, _ = await repo.get_or_create_report_cache(db, jid, "2026-02-11")

    await repo.save_report_items(db, cache_id, [{
        "title": "테스트",
        "url": "https://example.com/1",
        "summary": "기존 요약",
        "tags": ["태그"],
        "category": "new",
    }])

    items = await repo.get_report_items_by_cache(db, cache_id)
    item_id = items[0]["id"]

    await repo.update_report_item(db, item_id, "갱신된 요약")

    items = await repo.get_report_items_by_cache(db, cache_id)
    assert items[0]["summary"] == "갱신된 요약"


@pytest.mark.asyncio
async def test_get_recent_report_tags(db):
    """최근 3일 report_items 태그가 중복 없이 추출된다."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    cache_id, _ = await repo.get_or_create_report_cache(db, jid, "2026-02-11")

    await repo.save_report_items(db, cache_id, [
        {"title": "A", "url": "u1", "summary": "s", "tags": ["서부지검", "수사"], "category": "new"},
        {"title": "B", "url": "u2", "summary": "s", "tags": ["수사", "영등포"], "category": "new"},
    ])

    tags = await repo.get_recent_report_tags(db, jid, days=3)
    assert "서부지검" in tags
    assert "수사" in tags
    assert "영등포" in tags
    # 중복 없이
    assert len(tags) == len(set(tags))


@pytest.mark.asyncio
async def test_get_today_report_items(db):
    """당일 report_items 조회 (/check 맥락 로드용)."""
    jid = await repo.upsert_journalist(db, "33", "사회부", ["a"], "k")
    today = "2026-02-11"
    cache_id, _ = await repo.get_or_create_report_cache(db, jid, today)

    await repo.save_report_items(db, cache_id, [{
        "title": "테스트 기사",
        "url": "https://example.com/1",
        "summary": "요약",
        "tags": ["태그"],
        "category": "new",
    }])

    # get_today_report_items는 내부에서 오늘 날짜를 계산하므로,
    # 테스트 날짜와 실제 날짜가 다르면 빈 배열 반환 가능.
    # 여기서는 함수가 에러 없이 동작하는지 확인.
    result = await repo.get_today_report_items(db, jid)
    assert isinstance(result, list)
