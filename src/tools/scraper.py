"""네이버 뉴스 기사 본문 스크래퍼.

n.news.naver.com 기사 페이지에서 본문 첫 1~2문단을 추출한다.
Claude 분석 시 context 절약을 위해 전체 본문이 아닌 앞부분만 가져온다.
"""

import asyncio
import logging

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}
_MAX_PARAGRAPHS = 3


def _parse_article_body(html: str) -> str | None:
    """HTML에서 기사 본문 첫 1~2문단을 추출한다.

    네이버 뉴스 기사 컨테이너를 탐색하고, 내부 텍스트를 문단 단위로 분리하여
    앞부분만 반환한다.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 네이버 뉴스 기사 본문 컨테이너 탐색
    container = soup.select_one("article#dic_area") or soup.select_one(
        "div#newsct_article"
    )
    if container is None:
        return None

    # <p> 태그에서 문단 추출 시도
    paragraphs: list[str] = []
    for p_tag in container.find_all("p"):
        text = p_tag.get_text(strip=True)
        if text:
            paragraphs.append(text)
        if len(paragraphs) >= _MAX_PARAGRAPHS:
            break

    # <p> 태그가 없으면 컨테이너의 직접 텍스트를 줄바꿈 기준으로 분리
    if not paragraphs:
        raw_text = container.get_text(separator="\n", strip=True)
        for line in raw_text.split("\n"):
            line = line.strip()
            if line:
                paragraphs.append(line)
            if len(paragraphs) >= _MAX_PARAGRAPHS:
                break

    if not paragraphs:
        return None

    return "\n".join(paragraphs)


async def fetch_article_body(url: str) -> str | None:
    """단일 URL에서 기사 본문 첫 1~2문단을 가져온다.

    네트워크 오류, HTTP 오류, 파싱 실패 시 None을 반환한다.
    """
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _parse_article_body(resp.text)
    except Exception:
        logger.warning("기사 본문 추출 실패: %s", url, exc_info=True)
        return None


# 전역 동시 스크래핑 제한 (모든 파이프라인이 공유)
_scrape_semaphore = asyncio.Semaphore(50)


async def fetch_articles_batch(urls: list[str]) -> dict[str, str | None]:
    """여러 URL의 기사 본문을 병렬로 가져온다.

    httpx.AsyncClient를 공유하여 연결을 재사용하고,
    전역 세마포어로 동시 요청 수를 제한한다.
    """
    if not urls:
        return {}

    async def _fetch_one(client: httpx.AsyncClient, url: str) -> tuple[str, str | None]:
        async with _scrape_semaphore:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                body = _parse_article_body(resp.text)
            except Exception:
                logger.warning("기사 본문 추출 실패: %s", url, exc_info=True)
                body = None
            return url, body

    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
    ) as client:
        tasks = [_fetch_one(client, url) for url in urls]
        results = await asyncio.gather(*tasks)

    logger.info("본문 스크래핑 완료: %d건 중 %d건 성공", len(urls), sum(1 for _, b in results if b))
    return dict(results)
