"""Telegram 메시지 포맷팅 모듈.

/check, /report 결과를 기사 1건당 메시지 1개로 변환한다.
Telegram HTML 포맷을 사용하며, 메시지 최대 길이(4096자)를 초과하지 않도록 자른다.
"""

import html as html_module
from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))
_MAX_MSG_LEN = 4096


def _publisher_label(publisher: str, source_count: int) -> str:
    """언론사 표시 라벨. 복수 출처면 '[언론사 등 다수언론사]' 형식."""
    if not publisher:
        return ""
    if source_count > 1:
        return f"[{publisher} 등 다수]"
    return f"[{publisher}]"


def format_check_header(total: int, important: int, since: "datetime", now: "datetime") -> str:
    """헤더 메시지 (HTML). since~now 검색 범위를 표시한다."""
    since_kst = since.astimezone(_KST)
    now_kst = now.astimezone(_KST)
    # 같은 날이면 시각만, 다른 날이면 날짜+시각
    if since_kst.date() == now_kst.date():
        time_range = f"{since_kst.strftime('%H:%M')} ~ {now_kst.strftime('%H:%M')}"
    else:
        time_range = f"{since_kst.strftime('%Y-%m-%d %H:%M')} ~ {now_kst.strftime('%Y-%m-%d %H:%M')}"
    return (
        f"🔍 <b>타사 체크</b> ({time_range})\n"
        f"주요 <b>{important}</b>건 / 전체 {total}건"
    )


def format_article_message(article: dict) -> str:
    """기사 1건을 Telegram HTML 메시지로 포맷팅한다.

    article 키: category, publisher, title, summary, reason, url
    category: "exclusive" / "important"
    """
    category = article.get("category", "")
    tag_map = {"exclusive": "[단독]", "breaking": "[속보]"}
    tag = tag_map.get(category, "")
    publisher = html_module.escape(article.get("publisher", ""))
    title = html_module.escape(article.get("title", ""))
    # 코드가 [단독] 태그를 붙이는 경우, 제목 내 중복 제거
    if tag and title.startswith(tag):
        title = title[len(tag):].strip()
    summary = html_module.escape(article.get("summary", ""))
    reason = html_module.escape(article.get("reason", ""))
    url = article.get("url", "")
    pub_time = article.get("pub_time", "")
    source_count = article.get("source_count", 1)

    pub_label = _publisher_label(publisher, source_count)
    title_line = f"{tag} {pub_label} {title}".strip()
    if pub_time:
        title_line += f" ({pub_time})"

    if url:
        title_line = f'<a href="{html_module.escape(url)}">{title_line}</a>'
    lines = [
        f"<b>{title_line}</b>",
        "",
        summary,
    ]
    if reason:
        lines.append("")
        lines.append(f"-> <i>{reason}</i>")

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


def format_skipped_articles(skipped: list[dict], haiku_filtered: int = 0) -> list[str]:
    """스킵된 기사들을 제목+링크로 모아 메시지 목록으로 포맷팅한다.

    topic_cluster 기준으로 중복을 제거하여 동일 주제는 1건만 표시한다.
    4096자 초과 시 여러 메시지로 분할하여 HTML 태그가 깨지지 않도록 한다.
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

    if haiku_filtered > 0:
        header = f"<b>스킵 {len(deduped)}건</b> (사진/광고 기사 필터링 {haiku_filtered}건 외)"
    else:
        header = f"<b>스킵 {len(deduped)}건</b>"
    item_lines = []
    for article in deduped:
        publisher = html_module.escape(article.get("publisher", ""))
        title = html_module.escape(article.get("title", "")).strip()
        reason = html_module.escape(article.get("reason", "")).strip()
        url = article.get("url", "")
        pub_time = article.get("pub_time", "")
        source_count = article.get("source_count", 1)

        pub_label = _publisher_label(publisher, source_count)
        display = f"{pub_label} {title}".strip() if pub_label else (title or "(제목 없음)")
        if pub_time:
            display += f" ({pub_time})"
        link = f'<a href="{html_module.escape(url)}">{display}</a>' if url else display
        if reason:
            item_lines.append(f"- {link} → {reason}")
        else:
            item_lines.append(f"- {link}")
    return _split_blockquote_messages(header, item_lines)


# --- /report 포맷 ---

def _split_blockquote_messages(header: str, item_lines: list[str]) -> list[str]:
    """header + blockquote expandable 메시지를 4096자 이내로 분할한다.

    첫 메시지에 header를 포함하고, 이후 메시지는 blockquote만 사용한다.
    """
    bq_open = "<blockquote expandable>"
    bq_close = "</blockquote>"

    messages: list[str] = []
    current_lines: list[str] = []
    is_first = True

    for line in item_lines:
        prefix = f"{header}\n{bq_open}" if is_first else bq_open
        test_body = "\n".join(current_lines + [line])
        if len(prefix) + len(test_body) + len(bq_close) > _MAX_MSG_LEN and current_lines:
            # 현재까지 축적된 항목을 메시지로 확정
            body = "\n".join(current_lines)
            messages.append(f"{prefix}{body}{bq_close}")
            current_lines = [line]
            is_first = False
        else:
            current_lines.append(line)

    # 남은 항목
    if current_lines:
        prefix = f"{header}\n{bq_open}" if is_first else bq_open
        body = "\n".join(current_lines)
        messages.append(f"{prefix}{body}{bq_close}")

    return messages


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
    return (
        f"📋 <b>{label} 주요 뉴스</b> ({date})\n"
        f"주요 <b>{count}</b>건"
    )


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
        return (
            f"📋 <b>{label} 주요 뉴스</b> ({date})\n"
            f"총 <b>{total}</b>건 ({change_str})"
        )
    return (
        f"📋 <b>{label} 주요 뉴스</b> ({date})\n"
        f"총 <b>{total}</b>건 (변경 없음)"
    )


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
            tags.append("[신규]")
    if category == "follow_up":
        tags.append("[후속]")
    tag = " ".join(tags)

    publisher = html_module.escape(item.get("publisher", ""))
    title = html_module.escape(item.get("title", ""))
    # 코드가 [단독] 태그를 붙이는 경우, 제목 내 중복 제거
    if is_exclusive and title.startswith("[단독]"):
        title = title[len("[단독]"):].strip()
    summary = html_module.escape(item.get("summary", ""))
    reason = html_module.escape(item.get("reason", ""))
    pub_time = item.get("pub_time", "")
    url = item.get("url", "")
    prev_ref = item.get("prev_reference")
    source_count = item.get("source_count", 1)

    # 태그 + [언론사 등 N건] + 제목 + 시각
    pub_label = _publisher_label(publisher, source_count)
    title_part = f"{pub_label} {title}".strip() if pub_label else title
    header = f"{tag} {title_part}".strip() if tag else title_part
    if pub_time:
        header += f" ({pub_time})"
    if url:
        header = f'<a href="{html_module.escape(url)}">{header}</a>'
    lines = [
        f"<b>{header}</b>",
        "",
        summary,
    ]
    if reason:
        lines.append("")
        lines.append(f"-> <i>{reason}</i>")
    if prev_ref:
        lines.append("")
        lines.append(f"<i>(이전 전달: {html_module.escape(prev_ref)})</i>")

    return _truncate("\n".join(lines))


def format_unchanged_report_items(items: list[dict]) -> list[str]:
    """기보고 항목들을 제목+링크로 모아 토글 메시지 목록으로 포맷팅한다.

    4096자 초과 시 여러 메시지로 분할한다.
    """
    header = f"<b>기보고 {len(items)}건</b>"
    item_lines = []
    for item in items:
        publisher = html_module.escape(item.get("publisher", ""))
        title = html_module.escape(item.get("title", "")).strip()
        url = item.get("url", "")
        pub_time = item.get("pub_time", "")

        source_count = item.get("source_count", 1)
        pub_label = _publisher_label(publisher, source_count)
        display = f"{pub_label} {title}".strip() if pub_label else (title or "(제목 없음)")
        if pub_time:
            display += f" ({pub_time})"
        link = f'<a href="{html_module.escape(url)}">{display}</a>' if url else display
        item_lines.append(f"- {link}")
    return _split_blockquote_messages(header, item_lines)


