"""formatters 단위 테스트."""

from src.bot.formatters import format_check_header, format_article_message, format_no_results


def test_format_check_header():
    header = format_check_header(total=10, important=3)
    assert "주요 3건" in header
    assert "전체 10건 중" in header
    assert "타사 체크" in header


def test_format_article_exclusive():
    msg = format_article_message({
        "category": "exclusive",
        "publisher": "연합뉴스",
        "title": "서부지법 영장 기각",
        "summary": "서부지법이 구속영장을 기각했다.",
        "reason": "검찰 수사에 영향",
        "url": "https://example.com/1",
    })
    assert "[단독]" in msg
    assert "[연합뉴스]" in msg
    assert "서부지법 영장 기각" in msg
    assert "검찰 수사에 영향" in msg
    assert "https://example.com/1" in msg


def test_format_article_important():
    msg = format_article_message({
        "category": "important",
        "publisher": "한겨레",
        "title": "수사 확대",
        "summary": "임원 추가 소환",
        "reason": "새로운 전개",
        "url": "https://example.com/2",
    })
    assert "[주요]" in msg
    assert "[한겨레]" in msg


def test_format_article_truncation():
    """4096자 초과 시 잘린다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "테스트",
        "title": "제목",
        "summary": "A" * 5000,
        "reason": "",
        "url": "",
    })
    assert len(msg) <= 4096


def test_format_no_results():
    assert "신규 기사가 없습니다" in format_no_results()
