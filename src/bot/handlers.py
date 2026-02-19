"""/check, /report, /set_apikey, /set_division, /set_keyword 명령 핸들러."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

import anthropic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config import DEPARTMENTS, ADMIN_TELEGRAM_ID
from src.storage import repository as repo
from src.bot.formatters import (
    format_check_header, format_article_message, format_no_results,
    format_skipped_articles,
    format_report_header_a, format_report_header_b,
    format_report_item, format_unchanged_report_items,
)
from src.pipelines.check import run_check
from src.pipelines.report import run_report

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


def format_error_message(e: Exception) -> str:
    """예외를 사용자 친화적 한국어 메시지로 변환한다."""
    if isinstance(e, anthropic.APIStatusError):
        code = e.status_code
        if code == 529:
            return "Anthropic 서버 과부하로 요청 실패 (5회 재시도 모두 실패). 잠시 후 다시 시도해주세요."
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
                results, since, now, haiku_filtered = await run_check(db, journalist)
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
                results = await run_report(
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
            # 수정된 항목: 새 요약 + reason/exclusive 갱신
            merged = {**existing, "summary": mod["summary"], "action": "modified"}
            if mod.get("reason"):
                merged["reason"] = mod["reason"]
            if "exclusive" in mod:
                merged["exclusive"] = mod["exclusive"]
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
