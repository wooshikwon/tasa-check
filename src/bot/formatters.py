"""Telegram 메시지 포맷팅 모듈.

/check 결과를 기사 1건당 메시지 1개로 변환한다.
Telegram HTML 포맷을 사용하며, 메시지 최대 길이(4096자)를 초과하지 않도록 자른다.
"""

import html as html_module
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))
_MAX_MSG_LEN = 4096


def format_check_header(total: int, important: int) -> str:
    """헤더 메시지 (HTML)."""
    now = datetime.now(_KST)
    ts = now.strftime("%Y-%m-%d %H:%M")
    return (
        f"<b>타사 체크</b> ({ts})\n"
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
