"""Telegram 메시지 포맷팅 모듈.

/check 결과를 기사 1건당 메시지 1개로 변환한다.
Telegram 메시지 최대 길이(4096자)를 초과하지 않도록 자른다.
"""

from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))
_MAX_MSG_LEN = 4096


def format_check_header(total: int, important: int) -> str:
    """헤더 메시지: "타사 체크 (날짜 시각) - 주요 N건 (전체 M건 중)" """
    now = datetime.now(_KST)
    ts = now.strftime("%Y-%m-%d %H:%M")
    return f"타사 체크 ({ts}) - 주요 {important}건 (전체 {total}건 중)"


def format_article_message(article: dict) -> str:
    """기사 1건을 Telegram 메시지로 포맷팅한다.

    article 키: category, publisher, title, summary, reason, url
    category: "exclusive" / "important"
    """
    category = article.get("category", "")
    tag = "[단독]" if category == "exclusive" else "[주요]"
    publisher = article.get("publisher", "")
    title = article.get("title", "")
    summary = article.get("summary", "")
    reason = article.get("reason", "")
    url = article.get("url", "")

    lines = [
        f"{tag} [{publisher}] {title}",
        summary,
    ]
    if reason:
        lines.append(f"→ 중요 근거: {reason}")
    if url:
        lines.append(url)

    msg = "\n".join(lines)
    if len(msg) > _MAX_MSG_LEN:
        msg = msg[:_MAX_MSG_LEN - 3] + "..."
    return msg


def format_no_results() -> str:
    """수집 결과가 없을 때 메시지."""
    return "시간 윈도우 내 신규 기사가 없습니다."
