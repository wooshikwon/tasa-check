"""Telegram 메시지 포맷팅 모듈.

/check, /report 결과를 기사 1건당 메시지 1개로 변환한다.
Telegram HTML 포맷을 사용하며, 메시지 최대 길이(4096자)를 초과하지 않도록 자른다.
"""

import html as html_module
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))
_MAX_MSG_LEN = 4096


def format_check_header(total: int, important: int, since: "datetime", now: "datetime") -> str:
    """헤더 메시지 (HTML). since~now 검색 범위를 표시한다."""
    since_kst = since.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
    now_kst = now.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
    return (
        f"<b>타사 체크</b> ({since_kst} ~ {now_kst})\n"
        f"주요 <b>{important}</b>건 (전체 {total}건 중)"
    )


def format_article_message(article: dict) -> str:
    """기사 1건을 Telegram HTML 메시지로 포맷팅한다.

    article 키: category, publisher, title, summary, reason, article_urls
    category: "exclusive" / "important"
    """
    category = article.get("category", "")
    tag = "[단독]" if category == "exclusive" else "[주요]"
    publisher = html_module.escape(article.get("publisher", ""))
    title = html_module.escape(article.get("title", ""))
    summary = html_module.escape(article.get("summary", ""))
    reason = html_module.escape(article.get("reason", ""))

    # Claude 응답의 article_urls(리스트)에서 첫 번째 URL 추출
    urls = article.get("article_urls", [])
    url = urls[0] if urls else ""

    lines = [
        f"<b>{tag} [{publisher}] {title}</b>",
        "",
        summary,
    ]
    if reason:
        lines.append("")
        lines.append(f"-> <i>{reason}</i>")
    if url:
        lines.append("")
        lines.append(f'<a href="{html_module.escape(url)}">기사 원문</a>')

    msg = "\n".join(lines)
    if len(msg) > _MAX_MSG_LEN:
        msg = msg[:_MAX_MSG_LEN - 3] + "..."
    return msg


def format_no_results() -> str:
    """수집 결과가 없을 때 메시지."""
    return "시간 윈도우 내 신규 기사가 없습니다."


def format_no_important() -> str:
    """검색 결과는 있으나 주요 기사가 없을 때 메시지."""
    return "키워드 관련 주요 기사가 없습니다."


def format_skipped_articles(skipped: list[dict]) -> str:
    """스킵된 기사들을 제목+링크로 모아 하나의 메시지로 포맷팅한다.

    topic_cluster 기준으로 중복을 제거하여 동일 주제는 1건만 표시한다.
    """
    seen_clusters: set[str] = set()
    deduped: list[dict] = []
    for article in skipped:
        cluster = article.get("topic_cluster", "")
        if cluster and cluster in seen_clusters:
            continue
        if cluster:
            seen_clusters.add(cluster)
        deduped.append(article)

    lines = [f"<b>스킵 {len(deduped)}건</b>"]
    for article in deduped:
        title = html_module.escape(article.get("title", "")).strip()
        urls = article.get("article_urls", [])
        url = urls[0] if urls else ""
        if url:
            lines.append(f'- <a href="{html_module.escape(url)}">{title or "(제목 없음)"}</a>')
        else:
            lines.append(f"- {title or '(제목 없음)'}")
    return _truncate("\n".join(lines))


# --- /report 포맷 ---

def _truncate(msg: str) -> str:
    if len(msg) > _MAX_MSG_LEN:
        return msg[:_MAX_MSG_LEN - 3] + "..."
    return msg


def _dept_label(department: str) -> str:
    """부서명에 '부'가 없으면 붙인다."""
    return department if department.endswith("부") else f"{department}부"


def format_report_header_a(department: str, date: str, count: int) -> str:
    """시나리오 A 헤더: 당일 첫 요청."""
    label = _dept_label(department)
    return f"<b>{label} 주요 뉴스</b> ({date}) - 총 <b>{count}</b>건"


def format_report_header_b(department: str, date: str, total: int, modified: int, added: int) -> str:
    """시나리오 B 헤더: 당일 재요청. 총 건수 + 변경 내역."""
    label = _dept_label(department)
    if modified > 0 or added > 0:
        parts = []
        if modified > 0:
            parts.append(f"수정 {modified}건")
        if added > 0:
            parts.append(f"추가 {added}건")
        change_str = ", ".join(parts)
        return f"<b>{label} 주요 뉴스</b> ({date}) - 총 <b>{total}</b>건 ({change_str})"
    return f"<b>{label} 주요 뉴스</b> ({date}) - 총 <b>{total}</b>건 (변경 없음)"


def format_report_item(item: dict, scenario_b: bool = False) -> str:
    """브리핑 항목 1건을 Telegram HTML 메시지로 포맷팅한다."""
    category = item.get("category", "")
    action = item.get("action", "")

    # 태그 결정
    is_exclusive = item.get("exclusive", False)
    tags = []
    if is_exclusive:
        tags.append("[단독]")
    if scenario_b:
        if action == "modified":
            tags.append("[수정]")
        elif action == "added":
            tags.append("[추가]")
    if category == "follow_up":
        tags.append("[후속]")
    tag = " ".join(tags)

    title = html_module.escape(item.get("title", ""))
    summary = html_module.escape(item.get("summary", ""))
    url = item.get("url", "")
    prev_ref = item.get("prev_reference")

    header = f"{tag} {title}".strip() if tag else title
    lines = [
        f"<b>{header}</b>",
        "",
        summary,
    ]
    if prev_ref:
        lines.append("")
        lines.append(f"<i>(이전 전달: {html_module.escape(prev_ref)})</i>")
    if url:
        lines.append("")
        lines.append(f'<a href="{html_module.escape(url)}">기사 원문</a>')

    return _truncate("\n".join(lines))


def format_report_references(references: list[dict]) -> str:
    """참고 기사들을 제목+링크로 모아 하나의 메시지로 포맷팅한다."""
    lines = [f"<b>참고 {len(references)}건</b>"]
    for item in references:
        title = html_module.escape(item.get("title", ""))
        url = item.get("url", "")
        if url:
            lines.append(f'- <a href="{html_module.escape(url)}">{title}</a>')
        else:
            lines.append(f"- {title}")
    return _truncate("\n".join(lines))


def format_report_no_update() -> str:
    """시나리오 B 변경 없을 때 메시지."""
    return "업데이트 없음. 이전 브리핑과 동일합니다."
