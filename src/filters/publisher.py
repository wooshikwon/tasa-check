"""언론사 화이트리스트 기반 기사 필터 모듈."""

import json
from functools import lru_cache
from urllib.parse import urlparse

from src.config import BASE_DIR

_PUBLISHERS_PATH = BASE_DIR / "data" / "publishers.json"


@lru_cache(maxsize=1)
def load_publishers() -> list[dict]:
    """publishers.json에서 언론사 화이트리스트를 로드하고 캐싱한다."""
    with open(_PUBLISHERS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["publishers"]


def _extract_domain(url: str) -> str | None:
    """URL에서 호스트(도메인)를 추출한다."""
    try:
        parsed = urlparse(url)
        return parsed.hostname
    except Exception:
        return None


def _match_domain(article_domain: str, publisher_domain: str,
                   exclude_subdomains: list[str] | None = None) -> bool:
    """기사 도메인이 언론사 도메인에 속하는지 판별한다.

    정확히 일치하거나 서브도메인인 경우 매칭으로 판정한다.
    예: "news.chosun.com"은 "chosun.com"에 매칭된다.
    단, "notchosun.com"은 "chosun.com"에 매칭되지 않는다.
    exclude_subdomains에 포함된 도메인은 매칭에서 제외한다.
    예: "it.chosun.com"은 제외 목록에 있으면 매칭되지 않는다.
    """
    if exclude_subdomains and article_domain in exclude_subdomains:
        return False
    return (
        article_domain == publisher_domain
        or article_domain.endswith("." + publisher_domain)
    )


def get_publisher_name(url: str) -> str | None:
    """URL에 해당하는 언론사 이름을 반환한다. 화이트리스트에 없으면 None."""
    domain = _extract_domain(url)
    if domain is None:
        return None
    for pub in load_publishers():
        if _match_domain(domain, pub["domain"], pub.get("exclude_subdomains")):
            return pub["name"]
    return None


def filter_by_publisher(articles: list[dict]) -> list[dict]:
    """화이트리스트에 포함된 언론사의 기사만 필터링하여 반환한다."""
    publishers = load_publishers()
    result = []
    for article in articles:
        url = article.get("originallink", "")
        domain = _extract_domain(url)
        if domain is None:
            continue
        for pub in publishers:
            if _match_domain(domain, pub["domain"], pub.get("exclude_subdomains")):
                result.append(article)
                break
    return result
