"""파이프라인 공유 헬퍼.

handlers.py에서 추출한 기사 매칭/매핑 유틸리티.
check, report 파이프라인 모두에서 사용.
"""

import re

# 제목 정규화: 대괄호 태그 제거, 연속 공백 축소
_TITLE_BRACKET_RE = re.compile(r"\[[^\]]*\]\s*")

# 분석 가치 없는 제목 태그 (사진, 영상 등)
_SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}


def normalize_title(title: str) -> str:
    """매칭용 제목 정규화. [단독] 등 태그 제거, 공백 축소, 앞뒤 공백 제거."""
    return _TITLE_BRACKET_RE.sub("", title).strip()


def match_article(llm_title: str, articles: list[dict]) -> dict | None:
    """LLM이 반환한 제목으로 원본 기사를 매칭한다.

    1순위: 정확 일치
    2순위: 정규화 후 일치 (대괄호 태그 제거 등)
    3순위: 한쪽이 다른 쪽에 포함 (substring)
    """
    if not llm_title:
        return None

    # 1순위: 정확 일치
    for a in articles:
        if a["title"] == llm_title:
            return a

    # 2순위: 정규화 후 일치
    norm_llm = normalize_title(llm_title)
    if norm_llm:
        for a in articles:
            if normalize_title(a["title"]) == norm_llm:
                return a

    # 3순위: substring (짧은 쪽이 긴 쪽에 포함, 최소 15자 이상일 때만)
    if len(norm_llm) >= 15:
        for a in articles:
            norm_a = normalize_title(a["title"])
            if norm_a and (norm_llm in norm_a or norm_a in norm_llm):
                return a

    return None


def map_results_to_articles(
    results: list[dict],
    articles: list[dict],
    url_key: str = "url",
) -> None:
    """LLM 결과에 원본 기사의 URL, 언론사, 시각을 매핑한다.

    title 기반 매칭을 우선하고, 실패 시 source_indices 폴백을 사용하되
    폴백에서는 title을 덮어쓰지 않아 summary와의 일관성을 유지한다.
    """
    n = len(articles)
    for r in results:
        sources = r.pop("source_indices", [])
        merged = r.pop("merged_indices", [])
        valid_sources = [i for i in sources if 1 <= i <= n]
        valid_merged = [i for i in merged if 1 <= i <= n]

        r["source_count"] = len(valid_sources) + len(valid_merged)

        llm_title = r.get("title", "")
        matched = match_article(llm_title, articles)

        if matched:
            r["url"] = matched[url_key]
            r["publisher"] = matched["publisher"]
            r["title"] = matched["title"]
            r["source_count"] = max(r["source_count"], 1)
            pub_date = matched.get("pubDate", "")
            r["pub_time"] = pub_date.split(" ")[-1] if " " in pub_date else ""
        elif valid_sources:
            # source_indices 폴백: URL, 언론사만 가져오고 title은 유지
            src = articles[valid_sources[0] - 1]
            r["url"] = src[url_key]
            r["publisher"] = src["publisher"]
            # r["title"]은 LLM이 반환한 값 유지 (summary와 일관성 보장)
            pub_date = src.get("pubDate", "")
            r["pub_time"] = pub_date.split(" ")[-1] if " " in pub_date else ""
        else:
            r.setdefault("url", "")
            r.setdefault("publisher", "")
            r.setdefault("pub_time", "")


def has_skip_tag(title: str) -> bool:
    """제목에 스킵 태그(사진, 영상 등)가 포함되어 있는지 확인한다."""
    return any(tag in title for tag in _SKIP_TITLE_TAGS)
