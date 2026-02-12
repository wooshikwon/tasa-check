"""scraper 모듈 테스트.

n.news.naver.com 기사 페이지 구조를 모방한 HTML 픽스처로
본문 추출, 오류 처리, 배치 처리를 검증한다.
"""

import httpx
import pytest

from src.tools.scraper import (
    _parse_article_body,
    fetch_article_body,
    fetch_articles_batch,
)

# ---------------------------------------------------------------------------
# HTML 픽스처
# ---------------------------------------------------------------------------

# article#dic_area에 <p> 태그가 있는 일반적인 구조
ARTICLE_DIC_AREA_HTML = """
<html>
<body>
<div id="ct">
  <article id="dic_area">
    <p>서부지검 형사부는 15일 OO기업 대표를 피의자 신분으로 소환해 조사했다.</p>
    <p>검찰은 대표가 회사 자금 수십억원을 횡령한 혐의를 받고 있다고 밝혔다.</p>
    <p>이번 수사는 지난달 내부 고발로 시작됐으며 추가 소환 조사가 예정돼 있다.</p>
  </article>
</div>
</body>
</html>
"""

# div#newsct_article 컨테이너를 사용하는 구조
NEWSCT_ARTICLE_HTML = """
<html>
<body>
<div id="newsct_article">
  <p>영등포경찰서는 14일 긴급체포한 피의자를 검찰에 송치했다.</p>
  <p>피의자는 범행 일체를 부인하고 있는 것으로 알려졌다.</p>
</div>
</body>
</html>
"""

# <p> 태그 없이 텍스트만 있는 구조
NO_P_TAGS_HTML = """
<html>
<body>
<article id="dic_area">
국회 본회의에서 해당 법안이 가결됐다.
찬성 180표, 반대 92표로 통과됐다.
야당은 즉각 반발 성명을 냈다.
</article>
</body>
</html>
"""

# 기사 본문 컨테이너가 없는 페이지
NO_CONTAINER_HTML = """
<html>
<body>
<div id="unrelated">
  <p>관련 없는 내용</p>
</div>
</body>
</html>
"""

# 컨테이너는 있지만 텍스트가 없는 경우
EMPTY_CONTAINER_HTML = """
<html>
<body>
<article id="dic_area">
</article>
</body>
</html>
"""

# 문단이 1개뿐인 경우
SINGLE_PARAGRAPH_HTML = """
<html>
<body>
<article id="dic_area">
  <p>대법원은 이날 상고심에서 원심을 확정했다.</p>
</article>
</body>
</html>
"""

# 소제목 + 사진 + 본문 구조 (네이버 뉴스 전형적 패턴)
SUBHEADING_WITH_PHOTO_HTML = """
<html>
<body>
<article id="dic_area">
  <p><b>검찰, 대대적 수사 착수</b></p>
  <span class="end_photo_org"><img src="photo.jpg"><em>사진 캡션</em></span>
  <p>서울중앙지검 형사부는 15일 대규모 압수수색을 실시했다고 밝혔다.</p>
  <p>이번 수사는 지난달 금융감독원 고발로 시작됐으며 피의자 3명이 소환 예정이다.</p>
  <p>검찰 관계자는 "수사 범위를 확대할 계획"이라고 말했다.</p>
</article>
</body>
</html>
"""

# 특수 마커 소제목 포함
MARKER_SUBHEADING_HTML = """
<html>
<body>
<article id="dic_area">
  <p>▶ 수사 배경과 경위</p>
  <p>서울경찰청은 지난 10일부터 내사에 착수해 관련자 5명을 확인했다고 밝혔다.</p>
  <p>■ 향후 수사 계획</p>
  <p>경찰은 추가 압수수색과 관계자 소환을 계획하고 있는 것으로 전해졌다.</p>
  <p>이 사건은 시민단체의 고발로 수사가 시작된 것으로 알려졌다.</p>
</article>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# _parse_article_body 단위 테스트
# ---------------------------------------------------------------------------


class TestParseArticleBody:
    """HTML 파싱 및 문단 추출 검증."""

    def test_dic_area_p_tags(self):
        """article#dic_area의 <p> 태그에서 첫 3문단을 추출한다."""
        result = _parse_article_body(ARTICLE_DIC_AREA_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 3
        assert "서부지검" in paragraphs[0]
        assert "횡령" in paragraphs[1]
        assert "내부 고발" in paragraphs[2]

    def test_newsct_article_container(self):
        """div#newsct_article 컨테이너에서도 정상 추출한다."""
        result = _parse_article_body(NEWSCT_ARTICLE_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 2
        assert "영등포경찰서" in paragraphs[0]

    def test_no_p_tags_fallback(self):
        """<p> 태그가 없으면 텍스트를 줄바꿈 기준으로 분리하여 추출한다."""
        result = _parse_article_body(NO_P_TAGS_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 3
        assert "본회의" in paragraphs[0]
        assert "찬성" in paragraphs[1]
        assert "야당" in paragraphs[2]

    def test_no_container_returns_none(self):
        """기사 본문 컨테이너가 없으면 None을 반환한다."""
        assert _parse_article_body(NO_CONTAINER_HTML) is None

    def test_empty_container_returns_none(self):
        """컨테이너가 비어 있으면 None을 반환한다."""
        assert _parse_article_body(EMPTY_CONTAINER_HTML) is None

    def test_single_paragraph(self):
        """문단이 1개뿐이면 그 1개만 반환한다."""
        result = _parse_article_body(SINGLE_PARAGRAPH_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 1
        assert "대법원" in paragraphs[0]

    def test_skip_bold_subheading(self):
        """볼드 소제목과 사진 캡션을 건너뛰고 본문만 추출한다."""
        result = _parse_article_body(SUBHEADING_WITH_PHOTO_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 3
        # 소제목 "검찰, 대대적 수사 착수"가 포함되지 않아야 함
        assert "대대적 수사 착수" not in result
        assert "서울중앙지검" in paragraphs[0]

    def test_skip_marker_subheading(self):
        """▶, ■ 등 특수 마커 소제목을 건너뛰고 본문만 추출한다."""
        result = _parse_article_body(MARKER_SUBHEADING_HTML)
        assert result is not None
        paragraphs = result.split("\n")
        assert len(paragraphs) == 3
        # 마커 소제목이 포함되지 않아야 함
        assert "▶" not in result
        assert "■" not in result
        assert "서울경찰청" in paragraphs[0]


# ---------------------------------------------------------------------------
# fetch_article_body 테스트
# ---------------------------------------------------------------------------


class TestFetchArticleBody:
    """단일 URL 기사 본문 요청 검증."""

    async def test_success(self, httpx_mock):
        """정상 응답 시 본문 첫 2문단을 반환한다."""
        url = "https://n.news.naver.com/article/001/0001"
        httpx_mock.add_response(url=url, text=ARTICLE_DIC_AREA_HTML)

        result = await fetch_article_body(url)
        assert result is not None
        assert "서부지검" in result

    async def test_http_404(self, httpx_mock):
        """404 응답 시 None을 반환한다."""
        url = "https://n.news.naver.com/article/001/0002"
        httpx_mock.add_response(url=url, status_code=404)

        result = await fetch_article_body(url)
        assert result is None

    async def test_http_500(self, httpx_mock):
        """500 응답 시 None을 반환한다."""
        url = "https://n.news.naver.com/article/001/0003"
        httpx_mock.add_response(url=url, status_code=500)

        result = await fetch_article_body(url)
        assert result is None

    async def test_timeout(self, httpx_mock):
        """타임아웃 발생 시 None을 반환한다."""
        url = "https://n.news.naver.com/article/001/0004"
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=url)

        result = await fetch_article_body(url)
        assert result is None

    async def test_connection_error(self, httpx_mock):
        """연결 실패 시 None을 반환한다."""
        url = "https://n.news.naver.com/article/001/0005"
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=url)

        result = await fetch_article_body(url)
        assert result is None

    async def test_no_article_container(self, httpx_mock):
        """기사 컨테이너가 없는 HTML이면 None을 반환한다."""
        url = "https://n.news.naver.com/article/001/0006"
        httpx_mock.add_response(url=url, text=NO_CONTAINER_HTML)

        result = await fetch_article_body(url)
        assert result is None


# ---------------------------------------------------------------------------
# fetch_articles_batch 테스트
# ---------------------------------------------------------------------------


class TestFetchArticlesBatch:
    """배치 기사 본문 요청 검증."""

    async def test_multiple_success(self, httpx_mock):
        """여러 URL을 병렬 요청하여 결과를 dict로 반환한다."""
        url1 = "https://n.news.naver.com/article/001/0010"
        url2 = "https://n.news.naver.com/article/002/0010"
        httpx_mock.add_response(url=url1, text=ARTICLE_DIC_AREA_HTML)
        httpx_mock.add_response(url=url2, text=NEWSCT_ARTICLE_HTML)

        result = await fetch_articles_batch([url1, url2])
        assert len(result) == 2
        assert "서부지검" in result[url1]
        assert "영등포경찰서" in result[url2]

    async def test_partial_failure(self, httpx_mock):
        """일부 URL 실패 시 해당 URL만 None이고 나머지는 정상 반환한다."""
        url_ok = "https://n.news.naver.com/article/001/0011"
        url_fail = "https://n.news.naver.com/article/001/0012"
        httpx_mock.add_response(url=url_ok, text=ARTICLE_DIC_AREA_HTML)
        httpx_mock.add_response(url=url_fail, status_code=404)

        result = await fetch_articles_batch([url_ok, url_fail])
        assert result[url_ok] is not None
        assert result[url_fail] is None

    async def test_empty_urls(self):
        """빈 URL 목록이면 빈 dict를 반환한다."""
        result = await fetch_articles_batch([])
        assert result == {}

    async def test_all_failures(self, httpx_mock):
        """모든 URL이 실패하면 모든 값이 None이다."""
        url1 = "https://n.news.naver.com/article/001/0013"
        url2 = "https://n.news.naver.com/article/001/0014"
        httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=url1)
        httpx_mock.add_response(url=url2, status_code=500)

        result = await fetch_articles_batch([url1, url2])
        assert result[url1] is None
        assert result[url2] is None
