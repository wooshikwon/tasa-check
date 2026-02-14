"""/check, /report, /set_apikey, /set_division, /set_keyword 명령 핸들러."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

import anthropic

_KST = timezone(timedelta(hours=9))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config import (
    CHECK_MAX_WINDOW_SECONDS, REPORT_MAX_WINDOW_SECONDS,
    DEPARTMENTS, DEPARTMENT_PROFILES, ADMIN_TELEGRAM_ID,
)
from src.tools.search import search_news
from src.tools.scraper import fetch_articles_batch
from src.filters.publisher import filter_by_publisher, get_publisher_name
from src.agents.check_agent import analyze_articles, filter_check_articles
from src.agents.report_agent import filter_articles, analyze_report_articles
from src.storage import repository as repo
from src.bot.formatters import (
    format_check_header, format_article_message, format_no_results,
    format_skipped_articles,
    format_report_header_a, format_report_header_b,
    format_report_item, format_unchanged_report_items,
)

logger = logging.getLogger(__name__)

def format_error_message(e: Exception) -> str:
    """예외를 사용자 친화적 한국어 메시지로 변환한다."""
    if isinstance(e, anthropic.APIStatusError):
        code = e.status_code
        if code == 529:
            return "Anthropic 서버 과부하로 요청 실패 (3회 재시도 모두 실패). 잠시 후 다시 시도해주세요."
        if code == 429:
            return "API 요청 한도 초과. 잠시 후 다시 시도해주세요."
        if code == 401:
            return "API 키가 유효하지 않습니다. /set_apikey로 재설정해주세요."
        if code >= 500:
            return f"Anthropic 서버 오류({code}). 잠시 후 다시 시도해주세요."
    if isinstance(e, anthropic.APIConnectionError):
        return "Anthropic 서버 연결 실패. 네트워크 상태를 확인해주세요."
    if isinstance(e, anthropic.APITimeoutError):
        return "Anthropic 서버 응답 시간 초과. 잠시 후 다시 시도해주세요."
    if isinstance(e, RuntimeError):
        return str(e)
    return f"예상치 못한 오류: {type(e).__name__}"


# 사용자별 동시 실행 방지 잠금
_user_locks: dict[str, asyncio.Lock] = {}

# 전역 동시 파이프라인 제한 (1GB RAM 서버 OOM 방지)
_pipeline_semaphore = asyncio.Semaphore(5)


async def _run_check_pipeline(db, journalist: dict) -> tuple[list[dict] | None, datetime, datetime, int]:
    """네이버 검색 → 필터 → 본문 수집 → Claude 분석 파이프라인.

    Returns:
        (분석 결과 리스트, since, now, haiku_filtered). 기사가 없으면 결과는 None.
    """
    now = datetime.now(UTC)
    last_check = journalist["last_check_at"]
    if last_check:
        last_dt = datetime.fromisoformat(last_check).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), CHECK_MAX_WINDOW_SECONDS)
    else:
        window_seconds = CHECK_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)

    # 네이버 뉴스 수집 (Haiku 필터가 노이즈를 걸러주므로 400건까지 확대)
    raw_articles = await search_news(journalist["keywords"], since, max_results=300)
    if not raw_articles:
        return None, since, now, 0

    # 언론사 필터링
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        return None, since, now, 0

    # 제목 기반 필터링 (분석 가치 없는 기사 제거)
    _SKIP_TITLE_TAGS = {"[포토]", "[사진]", "[영상]", "[동영상]", "[화보]", "[카드뉴스]", "[인포그래픽]"}
    filtered = [
        a for a in filtered
        if not any(tag in a.get("title", "") for tag in _SKIP_TITLE_TAGS)
    ]
    if not filtered:
        return None, since, now, 0

    # Haiku 사전 필터 (키워드 관련성)
    pre_filter_count = len(filtered)
    filtered = await filter_check_articles(
        journalist["api_key"], filtered,
        journalist["keywords"], journalist["department"],
    )
    haiku_filtered = pre_filter_count - len(filtered)
    if not filtered:
        return None, since, now, haiku_filtered

    # 본문 수집 (Haiku 통과 기사만 스크래핑)
    urls = [a["link"] for a in filtered]
    bodies = await fetch_articles_batch(urls)

    # Claude 분석용 데이터 조립
    articles_for_analysis = []
    for a in filtered:
        publisher = get_publisher_name(a["originallink"]) or ""
        body = bodies.get(a["link"], "") or ""
        pub_date_str = a["pubDate"].strftime("%Y-%m-%d %H:%M") if hasattr(a["pubDate"], "strftime") else str(a["pubDate"])
        articles_for_analysis.append({
            "title": a["title"],
            "publisher": publisher,
            "body": body,
            "url": a["link"],
            "pubDate": pub_date_str,
        })

    # 이전 check 보고 이력 로드
    history = await repo.get_recent_reported_articles(db, journalist["id"], hours=72)

    # Claude API 분석
    results = await analyze_articles(
        api_key=journalist["api_key"],
        articles=articles_for_analysis,
        history=history,
        department=journalist["department"],
        keywords=journalist["keywords"],
    )

    # Claude는 기사 번호(index)만 반환 → 원본 데이터에서 URL, 언론사를 주입
    if results:
        n = len(articles_for_analysis)
        for r in results:
            sources = r.pop("source_indices", [])
            merged = r.pop("merged_indices", [])
            valid_sources = [i for i in sources if 1 <= i <= n]
            valid_merged = [i for i in merged if 1 <= i <= n]

            r["source_count"] = len(valid_sources) + len(valid_merged)
            if valid_sources:
                src = articles_for_analysis[valid_sources[0] - 1]
                r["url"] = src["url"]
                r["publisher"] = src["publisher"]
                r["title"] = src["title"]  # 원본 기사 제목 강제
                pub_date = src.get("pubDate", "")
                r["pub_time"] = pub_date.split(" ")[-1] if " " in pub_date else ""
            else:
                r.setdefault("url", "")
                r.setdefault("publisher", "")
                r.setdefault("pub_time", "")

    return results, since, now, haiku_filtered


async def _run_report_pipeline(
    db, journalist: dict, existing_items: list[dict] | None = None,
) -> list[dict] | None:
    """네이버 검색 → 언론사 필터 → LLM 필터 → 본문 수집 → Claude 분석 파이프라인.

    Returns:
        브리핑 항목 리스트. 수집 기사가 없으면 None.
    """
    now = datetime.now(UTC)
    last_report = journalist.get("last_report_at")
    if last_report:
        last_dt = datetime.fromisoformat(last_report).replace(tzinfo=UTC)
        window_seconds = min((now - last_dt).total_seconds(), REPORT_MAX_WINDOW_SECONDS)
    else:
        window_seconds = REPORT_MAX_WINDOW_SECONDS
    since = now - timedelta(seconds=window_seconds)
    department = journalist["department"]
    dept_label = department if department.endswith("부") else f"{department}부"

    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    report_keywords = profile.get("report_keywords", [])
    if not report_keywords:
        return None

    # 네이버 API 수집 (report는 400건 상한)
    raw_articles = await search_news(report_keywords, since, max_results=300)
    if not raw_articles:
        return None

    # 언론사 필터
    filtered = filter_by_publisher(raw_articles)
    if not filtered:
        return None

    # LLM 필터 (Haiku) — 제목+description 기반
    filtered = await filter_articles(journalist["api_key"], filtered, department)
    if not filtered:
        return None

    # 본문 수집 (첫 3문단)
    urls = [a["link"] for a in filtered]
    bodies = await fetch_articles_batch(urls)

    # 분석용 데이터 조립
    articles_for_analysis = []
    for a in filtered:
        publisher = get_publisher_name(a["originallink"]) or ""
        body = bodies.get(a["link"], "") or ""
        pub_date_str = (
            a["pubDate"].strftime("%Y-%m-%d %H:%M")
            if hasattr(a["pubDate"], "strftime")
            else str(a["pubDate"])
        )
        articles_for_analysis.append({
            "title": a["title"],
            "publisher": publisher,
            "body": body,
            "originallink": a["originallink"],
            "link": a["link"],
            "pubDate": pub_date_str,
        })

    # 이전 report 이력 (2일치)
    report_history = await repo.get_recent_report_items(db, journalist["id"])

    # Claude 분석
    results = await analyze_report_articles(
        api_key=journalist["api_key"],
        articles=articles_for_analysis,
        report_history=report_history,
        existing_items=existing_items,
        department=department,
    )

    # source_indices → URL, 언론사, 배포시각, 원본 제목 역매핑
    if results:
        n = len(articles_for_analysis)
        # 순번→DB ID 매핑 (시나리오 B)
        if existing_items:
            seq_to_db_id = {
                seq: item["id"]
                for seq, item in enumerate(existing_items, 1)
            }
        else:
            seq_to_db_id = {}

        for r in results:
            source_indices = r.pop("source_indices", [])
            valid_sources = [i for i in source_indices if 1 <= i <= n]
            r["source_count"] = len(valid_sources)
            if valid_sources:
                src = articles_for_analysis[valid_sources[0] - 1]
                r["url"] = src["link"]
                r["publisher"] = src["publisher"]
                r["title"] = src["title"]  # 원본 기사 제목 강제
                pub_date = src.get("pubDate", "")
                r["pub_time"] = pub_date.split(" ")[-1] if " " in pub_date else ""
            else:
                r.setdefault("url", "")
                r.setdefault("publisher", "")
                r.setdefault("pub_time", "")

            # 순번→DB ID 변환 (시나리오 B modified)
            if r.get("item_id") and seq_to_db_id:
                r["item_id"] = seq_to_db_id.get(r["item_id"], r["item_id"])

    return results


async def check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check 명령 처리. 설계 문서 8단계 흐름."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    # 동시 실행 방지
    lock = _user_locks.setdefault(telegram_id, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("이전 요청이 처리 중입니다. 완료 후 다시 시도해주세요.")
        return

    # [1] 프로필 로드
    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    async with lock:
        await update.message.reply_text("타사 체크 진행 중...")

        async with _pipeline_semaphore:
            try:
                results, since, now, haiku_filtered = await _run_check_pipeline(db, journalist)
            except Exception as e:
                logger.error("타사 체크 실패: %s", e, exc_info=True)
                await update.message.reply_text(f"타사 체크 실패: {format_error_message(e)}")
                return

        # check 실행 완료 시점에 항상 last_check_at 갱신
        await repo.update_last_check_at(db, journalist["id"])

        if results is None:
            await update.message.reply_text(format_no_results())
            return

        # 결과 저장 + 기사별 전송 (세마포어 해제 후)
        reported = [r for r in results if r["category"] != "skip"]
        skipped = [r for r in results if r["category"] == "skip"]

        await repo.save_reported_articles(db, journalist["id"], results)

        total = len(results) + haiku_filtered
        important = len(reported)
        await update.message.reply_text(
            format_check_header(total, important, since, now), parse_mode="HTML",
        )

        # 최신 기사 먼저 (pub_time desc)
        sorted_reported = sorted(reported, key=lambda r: r.get("pub_time", ""), reverse=True)
        for article in sorted_reported:
            msg = format_article_message(article)
            await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)

        if skipped:
            for msg in format_skipped_articles(skipped, haiku_filtered):
                await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report 명령 처리. 부서 뉴스 브리핑."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    # 동시 실행 방지
    lock = _user_locks.setdefault(telegram_id, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("이전 요청이 처리 중입니다. 완료 후 다시 시도해주세요.")
        return

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    async with lock:
        dept = journalist["department"]
        dept_label = dept if dept.endswith("부") else f"{dept}부"
        await update.message.reply_text(f"오늘 {dept_label} 브리핑 생성 중...")

        today = datetime.now(_KST).strftime("%Y-%m-%d")
        department = journalist["department"]

        cache_id, is_new = await repo.get_or_create_report_cache(db, journalist["id"], today)

        existing_items = []
        if not is_new:
            existing_items = await repo.get_report_items_by_cache(db, cache_id)

        is_scenario_a = is_new or len(existing_items) == 0

        async with _pipeline_semaphore:
            try:
                results = await _run_report_pipeline(
                    db, journalist,
                    existing_items=existing_items if not is_scenario_a else None,
                )
            except Exception as e:
                logger.error("report 파이프라인 실패: %s", e, exc_info=True)
                await update.message.reply_text(f"브리핑 생성 실패: {format_error_message(e)}")
                return

        # report 실행 완료 시점에 항상 last_report_at 갱신
        await repo.update_last_report_at(db, journalist["id"])

        if results is None:
            await update.message.reply_text("관련 뉴스를 찾지 못했습니다.")
            return

        # 결과 전송 (세마포어 해제 후)
        if is_scenario_a:
            await _handle_report_scenario_a(
                update.message.reply_text, db, cache_id, department, today, results,
            )
        else:
            await _handle_report_scenario_b(
                update.message.reply_text, db, cache_id, department, today,
                existing_items, results,
            )


async def _handle_report_scenario_a(
    send_fn, db, cache_id, department, today, results,
) -> None:
    """시나리오 A: 당일 첫 요청. 전체 브리핑 생성."""
    if results:
        await repo.save_report_items(db, cache_id, results)

    if not results:
        await send_fn("관련 뉴스를 찾지 못했습니다.")
        return

    # 최신 기사 먼저 (pub_time desc)
    sorted_results = sorted(results, key=lambda r: r.get("pub_time", ""), reverse=True)

    await send_fn(
        format_report_header_a(department, today, len(sorted_results)),
        parse_mode="HTML",
    )
    for item in sorted_results:
        msg = format_report_item(item)
        await send_fn(msg, parse_mode="HTML", disable_web_page_preview=True)


async def _handle_report_scenario_b(
    send_fn, db, cache_id, department, today,
    existing_items, delta_results,
) -> None:
    """시나리오 B: 당일 재요청. 기존 항목 + 변경분을 합쳐 전체 브리핑 출력."""
    # action 필드 보정
    for r in delta_results:
        if "action" not in r:
            r["action"] = "added"

    # 기존 항목에 수정 반영
    modified_ids: set[int] = set()
    delta_by_item_id = {}
    for r in delta_results:
        if r.get("action") == "modified" and r.get("item_id"):
            delta_by_item_id[r["item_id"]] = r

    merged_items: list[dict] = []
    for existing in existing_items:
        mod = delta_by_item_id.get(existing["id"])
        if mod:
            # 수정된 항목: 새 요약 + reason/exclusive/key_facts 갱신
            merged = {**existing, "summary": mod["summary"], "action": "modified"}
            if mod.get("reason"):
                merged["reason"] = mod["reason"]
            if "exclusive" in mod:
                merged["exclusive"] = mod["exclusive"]
            if mod.get("key_facts"):
                merged["key_facts"] = mod["key_facts"]
            merged_items.append(merged)
            modified_ids.add(existing["id"])
        else:
            # 변경 없는 항목
            merged_items.append({**existing, "action": "unchanged"})

    # 추가 항목 병합
    added = [r for r in delta_results if r.get("action") == "added"]
    merged_items.extend(added)

    # DB 반영
    if added:
        await repo.save_report_items(db, cache_id, added)
    for item_id, mod in delta_by_item_id.items():
        await repo.update_report_item(
            db, item_id, mod["summary"],
            reason=mod.get("reason"),
            exclusive=mod.get("exclusive"),
            key_facts=mod.get("key_facts"),
        )

    # 변경 건수 계산
    modified_count = len(modified_ids)
    added_count = len(added)

    # 헤더 전송
    await send_fn(
        format_report_header_b(department, today, len(merged_items), modified_count, added_count),
        parse_mode="HTML",
    )

    # 변경 항목(modified/added)과 기보고 항목(unchanged) 분리
    changed = [r for r in merged_items if r.get("action") in ("modified", "added")]
    unchanged = [r for r in merged_items if r.get("action") == "unchanged"]

    # 변경 항목: 최신 기사 먼저 개별 전송
    changed.sort(key=lambda r: r.get("pub_time", ""), reverse=True)
    for item in changed:
        msg = format_report_item(item, scenario_b=True)
        await send_fn(msg, parse_mode="HTML", disable_web_page_preview=True)

    # 기보고 항목: 토글 메시지로 모아 전송
    if unchanged:
        for msg in format_unchanged_report_items(unchanged):
            await send_fn(msg, parse_mode="HTML", disable_web_page_preview=True)


async def set_division_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_division 명령 처리. 부서 변경 InlineKeyboard 표시."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    keyboard = [
        DEPARTMENTS[i:i+2] for i in range(0, len(DEPARTMENTS), 2)
    ]
    keyboard = [
        [InlineKeyboardButton(dept, callback_data=f"setdiv:{dept}") for dept in row]
        for row in keyboard
    ]
    await update.message.reply_text(
        f"현재 부서: {journalist['department']}\n변경할 부서를 선택해주세요.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_division_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """부서 변경 InlineKeyboard 콜백 처리."""
    query = update.callback_query
    await query.answer()

    new_dept = query.data.removeprefix("setdiv:")
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await query.edit_message_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    if journalist["department"] == new_dept:
        await query.edit_message_text(f"이미 {new_dept} 소속입니다.")
        return

    # 부서 변경 + check/report 이력 삭제 (스케줄 유지)
    await repo.update_department(db, telegram_id, new_dept)
    await repo.clear_journalist_data(db, journalist["id"])

    await query.edit_message_text(
        f"부서가 {new_dept}(으)로 변경되었습니다.\n"
        f"이전 체크/브리핑 이력이 초기화되었습니다."
    )


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status 명령 처리. 현재 설정 표시."""
    db = context.bot_data["db"]
    telegram_id = str(update.effective_user.id)

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        await update.message.reply_text("프로필이 없습니다. /start로 등록해주세요.")
        return

    dept = journalist["department"]
    dept_label = dept if dept.endswith("부") else f"{dept}부"
    keywords = ", ".join(journalist["keywords"]) if journalist["keywords"] else "(없음)"

    # API Key 마스킹 (앞 7자리 + ****)
    api_key = journalist.get("api_key", "")
    masked_key = f"{api_key[:7]}****" if len(api_key) >= 7 else "(미설정)"

    # 스케줄 조회
    schedules = await repo.get_schedules(db, journalist["id"])
    check_times = [s["time_kst"] for s in schedules if s["command"] == "check"]
    report_times = [s["time_kst"] for s in schedules if s["command"] == "report"]

    check_sched = ", ".join(check_times) if check_times else "(없음)"
    report_sched = ", ".join(report_times) if report_times else "(없음)"

    lines = [
        "현재 설정:",
        f"  부서: {dept_label}",
        f"  키워드: {keywords}",
        f"  API Key: {masked_key}",
        "",
        f"  check 스케줄: {check_sched}",
        f"  report 스케줄: {report_sched}",
    ]
    await update.message.reply_text("\n".join(lines))


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats 관리자 전용 통계 조회."""
    telegram_id = str(update.effective_user.id)
    if telegram_id != ADMIN_TELEGRAM_ID:
        return

    db = context.bot_data["db"]
    stats = await repo.get_admin_stats(db)

    lines = [f"전체 사용자: {stats['total_users']}명"]

    # 부서별
    if stats["dept_stats"]:
        lines.append("")
        lines.append("[부서별]")
        for dept, cnt in stats["dept_stats"]:
            lines.append(f"  {dept}: {cnt}명")

    # 스케줄
    check_cnt = stats["schedule_stats"].get("check", 0)
    report_cnt = stats["schedule_stats"].get("report", 0)
    lines.append("")
    lines.append(f"[스케줄] {stats['schedule_users']}명 등록")
    if check_cnt or report_cnt:
        lines.append(f"  check {check_cnt}건 / report {report_cnt}건")

    # 사용자 목록
    lines.append("")
    lines.append("[사용자]")
    for u in stats["users"]:
        kw = ", ".join(u["keywords"])
        sched = f" | 스케줄 {u['schedule_count']}건" if u["schedule_count"] else ""
        last = ""
        if u["last_check_at"]:
            utc_dt = datetime.fromisoformat(u["last_check_at"]).replace(tzinfo=UTC)
            kst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
            last = f" | 최근 check: {kst_dt.strftime('%Y-%m-%d %H:%M')}"
        lines.append(f"  {u['department']} | {kw}{sched}{last}")

    await update.message.reply_text("\n".join(lines))
