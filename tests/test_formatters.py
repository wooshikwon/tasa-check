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
        "url": "https://example.com/1",
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
        "url": "https://example.com/2",
    })
    assert "[단독]" not in msg
    assert "[속보]" not in msg
    assert "한겨레" in msg


def test_format_article_multi_source():
    """source_count > 1이면 '[언론사 등 N건]' 형식으로 표시된다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "헤럴드경제",
        "title": "수사 확대",
        "summary": "요약",
        "reason": "",
        "url": "https://example.com/1",
        "source_count": 3,
    })
    assert "[헤럴드경제 등 3건]" in msg


def test_format_article_no_url():
    """url이 없으면 링크 없이 출력된다."""
    msg = format_article_message({
        "category": "important",
        "publisher": "테스트",
        "title": "제목",
        "summary": "요약",
        "reason": "",
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


# --- 메시지 분할 ---

from src.bot.formatters import format_skipped_articles, format_unchanged_report_items


def test_format_skipped_articles_returns_list():
    """반환 타입이 list[str]이다."""
    skipped = [{"title": "기사1", "publisher": "A", "url": "https://ex.com/1", "reason": "사유"}]
    result = format_skipped_articles(skipped)
    assert isinstance(result, list)
    assert len(result) == 1
    assert "<blockquote expandable>" in result[0]
    assert "스킵 1건" in result[0]


def test_format_skipped_articles_dedup():
    """topic_cluster 기준 중복 제거."""
    skipped = [
        {"title": "기사1", "publisher": "A", "url": "", "topic_cluster": "사건X"},
        {"title": "기사2", "publisher": "B", "url": "", "topic_cluster": "사건X"},
        {"title": "기사3", "publisher": "C", "url": "", "topic_cluster": "사건Y"},
    ]
    result = format_skipped_articles(skipped)
    full = "\n".join(result)
    assert "스킵 2건" in full
    assert "기사1" in full
    assert "기사2" not in full
    assert "기사3" in full


def test_format_skipped_articles_split_on_overflow():
    """4096자 초과 시 여러 메시지로 분할된다."""
    # 긴 제목 + URL로 항목당 ~200자 → 30건이면 ~6000자 → 분할 필요
    skipped = [
        {
            "title": f"매우 긴 제목 기사 번호 {i} {'가' * 50}",
            "publisher": "테스트언론사",
            "url": f"https://news.naver.com/article/very-long-path-segment-{i}",
            "reason": f"사유 {i}번 입니다",
            "topic_cluster": f"주제{i}",
        }
        for i in range(30)
    ]
    result = format_skipped_articles(skipped)
    assert len(result) >= 2, f"분할되어야 하지만 {len(result)}개 메시지"
    for msg in result:
        assert len(msg) <= 4096, f"메시지 길이 초과: {len(msg)}"
        assert "</blockquote>" in msg  # HTML 태그 정상 닫힘
    # 첫 메시지에만 헤더
    assert "스킵" in result[0]


def test_format_skipped_articles_all_messages_valid_html():
    """분할된 모든 메시지의 blockquote 태그가 정상 닫힌다."""
    skipped = [
        {
            "title": f"기사{i}",
            "publisher": "언론",
            "url": f"https://ex.com/{i}",
            "reason": "사유" * 20,
            "topic_cluster": f"t{i}",
        }
        for i in range(50)
    ]
    result = format_skipped_articles(skipped)
    for msg in result:
        assert msg.count("<blockquote expandable>") == msg.count("</blockquote>")


def test_format_unchanged_report_items_returns_list():
    """반환 타입이 list[str]이다."""
    items = [{"title": "기사1", "publisher": "A", "url": "https://ex.com/1"}]
    result = format_unchanged_report_items(items)
    assert isinstance(result, list)
    assert len(result) == 1
    assert "기보고 1건" in result[0]


def test_format_unchanged_report_items_split():
    """4096자 초과 시 분할된다."""
    items = [
        {
            "title": f"기보고 기사 {i} {'나' * 60}",
            "publisher": "테스트",
            "url": f"https://news.naver.com/article/path-{i}",
        }
        for i in range(40)
    ]
    result = format_unchanged_report_items(items)
    assert len(result) >= 2
    for msg in result:
        assert len(msg) <= 4096
        assert "</blockquote>" in msg
