"""언론사 화이트리스트 필터 테스트."""

import pytest

from src.filters.publisher import (
    filter_by_publisher,
    get_publisher_name,
    load_publishers,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """각 테스트 전후로 캐시를 초기화하여 테스트 격리를 보장한다."""
    load_publishers.cache_clear()
    yield
    load_publishers.cache_clear()


# -- load_publishers --


class TestLoadPublishers:
    def test_returns_27_publishers(self):
        publishers = load_publishers()
        assert len(publishers) == 27

    def test_publisher_has_required_fields(self):
        publishers = load_publishers()
        for pub in publishers:
            assert "name" in pub
            assert "domain" in pub
            assert "category" in pub

    def test_cache_returns_same_object(self):
        """lru_cache가 동일 객체를 반환하는지 확인한다."""
        first = load_publishers()
        second = load_publishers()
        assert first is second


# -- get_publisher_name --


class TestGetPublisherName:
    def test_exact_domain_match(self):
        """정확한 도메인 일치."""
        assert get_publisher_name("https://chosun.com/article/123") == "조선일보"

    def test_subdomain_match(self):
        """서브도메인이 포함된 URL도 매칭된다."""
        assert get_publisher_name("https://news.chosun.com/article/123") == "조선일보"

    def test_www_subdomain_match(self):
        assert get_publisher_name("https://www.donga.com/news/article") == "동아일보"

    def test_deep_subdomain_match(self):
        """다단계 서브도메인도 매칭된다."""
        assert get_publisher_name("https://sports.news.chosun.com/a") == "조선일보"

    def test_publisher_domain_is_subdomain(self):
        """언론사 도메인 자체가 서브도메인인 경우 (예: news.kbs.co.kr)."""
        assert get_publisher_name("https://news.kbs.co.kr/article") == "KBS"

    def test_no_match_returns_none(self):
        assert get_publisher_name("https://example.com/article") is None

    def test_similar_domain_no_false_positive(self):
        """도메인 경계를 벗어난 유사 도메인은 매칭되지 않는다."""
        assert get_publisher_name("https://notchosun.com/article") is None

    def test_malformed_url_returns_none(self):
        assert get_publisher_name("not-a-url") is None

    def test_empty_string_returns_none(self):
        assert get_publisher_name("") is None

    def test_all_publishers_matchable(self):
        """등록된 23개 언론사 도메인이 모두 매칭 가능한지 전수 검증한다."""
        for pub in load_publishers():
            url = f"https://{pub['domain']}/article/test"
            assert get_publisher_name(url) == pub["name"], (
                f"{pub['name']}({pub['domain']}) 매칭 실패"
            )


# -- filter_by_publisher --


class TestFilterByPublisher:
    def test_keeps_whitelisted_articles(self):
        articles = [
            {"title": "조선 기사", "originallink": "https://www.chosun.com/a/1"},
            {"title": "동아 기사", "originallink": "https://news.donga.com/a/2"},
        ]
        result = filter_by_publisher(articles)
        assert len(result) == 2

    def test_removes_non_whitelisted_articles(self):
        articles = [
            {"title": "블로그", "originallink": "https://blog.example.com/post"},
        ]
        result = filter_by_publisher(articles)
        assert len(result) == 0

    def test_mixed_articles(self):
        articles = [
            {"title": "조선 기사", "originallink": "https://www.chosun.com/a/1"},
            {"title": "블로그", "originallink": "https://blog.example.com/post"},
            {"title": "JTBC 기사", "originallink": "https://news.jtbc.co.kr/a/3"},
        ]
        result = filter_by_publisher(articles)
        assert len(result) == 2
        titles = [a["title"] for a in result]
        assert "조선 기사" in titles
        assert "JTBC 기사" in titles
        assert "블로그" not in titles

    def test_empty_list(self):
        assert filter_by_publisher([]) == []

    def test_missing_originallink_key(self):
        """originallink 키가 없는 기사는 건너뛴다."""
        articles = [
            {"title": "키 누락"},
        ]
        result = filter_by_publisher(articles)
        assert len(result) == 0

    def test_malformed_url_skipped(self):
        articles = [
            {"title": "잘못된 URL", "originallink": "not-a-url"},
            {"title": "정상 기사", "originallink": "https://www.hani.co.kr/a/1"},
        ]
        result = filter_by_publisher(articles)
        assert len(result) == 1
        assert result[0]["title"] == "정상 기사"

    def test_preserves_article_data(self):
        """필터링 후에도 기사의 모든 필드가 보존된다."""
        original = {
            "title": "테스트",
            "originallink": "https://www.mk.co.kr/news/123",
            "description": "설명",
            "pubDate": "2025-01-15",
        }
        result = filter_by_publisher([original])
        assert len(result) == 1
        assert result[0] is original

    def test_preserves_order(self):
        """입력 순서가 유지된다."""
        articles = [
            {"title": "B", "originallink": "https://www.donga.com/a/2"},
            {"title": "A", "originallink": "https://www.chosun.com/a/1"},
            {"title": "C", "originallink": "https://www.hani.co.kr/a/3"},
        ]
        result = filter_by_publisher(articles)
        assert [a["title"] for a in result] == ["B", "A", "C"]
