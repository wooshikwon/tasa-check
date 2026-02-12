"""formatters 단위 테스트."""

from datetime import datetime, timezone

from src.bot.formatters import format_check_header, format_article_message, format_no_results


def test_format_check_header():
    since = datetime(2026, 2, 11, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 2, 11, 3, 0, tzinfo=timezone.utc)
    header = format_check_header(total=10, important=3, since=since, now=now)
    assert "주요" in header
    assert "3" in header
    assert "전체 10건 중" in header
    assert "타사 체크" in header


def test_format_article_exclusive():
    msg = format_article_message({
        "category": "exclusive",
        "publisher": "연합뉴스",
        "title": "서부지법 영장 기각",
        "summary": "서부지법이 구속영장을 기각했다.",
        "reason": "검찰 수사에 영향",
        "article_urls": ["https://example.com/1"],
    })
    assert "[단독]" in msg
    assert "연합뉴스" in msg
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
        "article_urls": ["https://example.com/2"],
    })
    assert "[단독]" not in msg
    assert "[속보]" not in msg
    assert "한겨레" in msg


def test_format_article_no_urls():
    """article_urls가 빈 배열이면 링크 없이 출력된다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "테스트",
        "title": "제목",
        "summary": "요약",
        "reason": "",
        "article_urls": [],
    })
    assert "기사 원문" not in msg


def test_format_article_truncation():
    """4096자 초과 시 잘린다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "테스트",
        "title": "제목",
        "summary": "A" * 5000,
        "reason": "",
        "article_urls": [],
    })
    assert len(msg) <= 4096


def test_format_article_html_escaping():
    """HTML 특수문자가 이스케이프된다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "A&B",
        "title": "<script>alert</script>",
        "summary": "본문",
        "reason": "",
        "article_urls": [],
    })
    assert "<script>" not in msg
    assert "&amp;" in msg


def test_format_no_results():
    assert "신규 기사가 없습니다" in format_no_results()


# --- /report 포맷 ---

from src.bot.formatters import (
    format_report_header_a,
    format_report_header_b,
    format_report_item,
)


def test_format_report_header_a():
    header = format_report_header_a("사회", "2026-02-11", 7)
    assert "사회부 주요 뉴스" in header
    assert "2026-02-11" in header
    assert "7" in header


def test_format_report_header_b():
    header = format_report_header_b("사회", "2026-02-11", 10, 2, 1)
    assert "사회부 주요 뉴스" in header
    assert "수정" in header
    assert "추가" in header


def test_format_report_header_b_no_change():
    header = format_report_header_b("사회", "2026-02-11", 5, 0, 0)
    assert "변경 없음" in header


def test_format_report_item_scenario_a_new():
    """category=new, reason 포함 시 선별 사유가 표시된다."""
    msg = format_report_item({
        "title": "신규 기사 제목",
        "summary": "기사 요약 내용",
        "reason": "핵심 수사 진전",
        "url": "https://example.com/1",
        "category": "new",
    })
    assert "신규 기사 제목" in msg
    assert "https://example.com/1" in msg
    assert "핵심 수사 진전" in msg
    assert "->" in msg


def test_format_report_item_scenario_a_no_reason():
    """reason이 없으면 사유 줄이 생략된다."""
    msg = format_report_item({
        "title": "기사 제목",
        "summary": "요약",
        "url": "https://example.com/1",
        "category": "new",
    })
    assert "->" not in msg


def test_format_report_item_scenario_a_follow_up():
    msg = format_report_item({
        "title": "후속 기사",
        "summary": "후속 요약",
        "url": "https://example.com/2",
        "category": "follow_up",
        "prev_reference": '2026-02-10 "이전 기사"',
    })
    assert "[후속]" in msg
    assert "이전 전달:" in msg


def test_format_report_item_exclusive():
    """exclusive=True면 [단독] 태그가 표시된다."""
    msg = format_report_item({
        "title": "단독 기사",
        "summary": "요약",
        "url": "https://example.com/1",
        "category": "new",
        "exclusive": True,
    })
    assert "[단독]" in msg


def test_format_report_item_scenario_b():
    msg = format_report_item({
        "action": "modified",
        "title": "수정 기사",
        "summary": "갱신된 요약",
        "url": "https://example.com/3",
        "category": "new",
    }, scenario_b=True)
    assert "[수정]" in msg
    assert "수정 기사" in msg
