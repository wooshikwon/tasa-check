"""스케줄 자동 실행 — JobQueue 콜백 + 서버 시작 시 복원."""

import asyncio
import logging

from datetime import datetime, time, timedelta, timezone

from telegram.ext import Application, ContextTypes

from src.storage import repository as repo
from src.bot.handlers import (
    _run_check_pipeline,
    _run_report_pipeline,
    _user_locks,
    _pipeline_semaphore,
    _handle_report_scenario_a,
    _handle_report_scenario_b,
)
from src.bot.formatters import (
    format_check_header,
    format_article_message,
    format_no_results,
    format_skipped_articles,
)

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


# --- JobQueue 콜백 ---

async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue에서 호출되는 자동 check 실행."""
    job = context.job
    chat_id = job.chat_id
    journalist_id = job.data["journalist_id"]
    telegram_id = str(chat_id)
    db = context.bot_data["db"]

    lock = _user_locks.setdefault(telegram_id, asyncio.Lock())
    if lock.locked():
        return

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        return

    async def send_fn(text, **kwargs):
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

    async with lock:
        await send_fn("━━━━━━━━━━━━━━━━━━━━\n⏰ 자동 타사체크\n━━━━━━━━━━━━━━━━━━━━")

        async with _pipeline_semaphore:
            try:
                results, since, now, haiku_filtered = await _run_check_pipeline(db, journalist)
            except Exception as e:
                logger.error("자동 check 실패 (journalist=%d): %s", journalist_id, e, exc_info=True)
                await send_fn(f"[자동 체크] 오류: {e}")
                return

        # check 실행 완료 시점에 항상 last_check_at 갱신
        await repo.update_last_check_at(db, journalist["id"])

        if results is None:
            await send_fn(format_no_results())
            return

        reported = [r for r in results if r["category"] != "skip"]
        skipped = [r for r in results if r["category"] == "skip"]

        await repo.save_reported_articles(db, journalist["id"], results)

        total = len(results) + haiku_filtered
        await send_fn(
            format_check_header(total, len(reported), since, now),
            parse_mode="HTML",
        )
        sorted_reported = sorted(reported, key=lambda r: r.get("pub_time", ""), reverse=True)
        for article in sorted_reported:
            await send_fn(
                format_article_message(article),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        if skipped:
            for msg in format_skipped_articles(skipped, haiku_filtered):
                await send_fn(msg, parse_mode="HTML", disable_web_page_preview=True)


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue에서 호출되는 자동 report 실행."""
    job = context.job
    chat_id = job.chat_id
    journalist_id = job.data["journalist_id"]
    telegram_id = str(chat_id)
    db = context.bot_data["db"]

    lock = _user_locks.setdefault(telegram_id, asyncio.Lock())
    if lock.locked():
        return

    journalist = await repo.get_journalist(db, telegram_id)
    if not journalist:
        return

    async def send_fn(text, **kwargs):
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

    async with lock:
        await send_fn("━━━━━━━━━━━━━━━━━━━━\n⏰ 자동 브리핑\n━━━━━━━━━━━━━━━━━━━━")

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
                logger.error("자동 report 실패 (journalist=%d): %s", journalist_id, e, exc_info=True)
                await send_fn(f"[자동 브리핑] 오류: {e}")
                return

        # report 실행 완료 시점에 항상 last_report_at 갱신
        await repo.update_last_report_at(db, journalist["id"])

        if results is None:
            await send_fn("관련 뉴스를 찾지 못했습니다.")
            return

        # 결과 전송 (세마포어 해제 후)
        if is_scenario_a:
            await _handle_report_scenario_a(
                send_fn, db, cache_id, department, today, results,
            )
        else:
            await _handle_report_scenario_b(
                send_fn, db, cache_id, department, today,
                existing_items, results,
            )


# --- JobQueue 등록/해제 ---

def register_job(
    app: Application,
    command: str,
    journalist_id: int,
    telegram_id: str,
    time_kst_str: str,
) -> None:
    """하나의 스케줄 job을 JobQueue에 등록한다."""
    callback = scheduled_check if command == "check" else scheduled_report
    h, m = map(int, time_kst_str.split(":"))
    job_time = time(hour=h, minute=m, tzinfo=_KST)
    job_name = f"{command}_{journalist_id}_{time_kst_str}"

    app.job_queue.run_daily(
        callback,
        time=job_time,
        chat_id=int(telegram_id),
        name=job_name,
        data={"journalist_id": journalist_id},
    )


def unregister_jobs(
    app: Application,
    journalist_id: int,
    command: str | None = None,
) -> None:
    """사용자의 스케줄 job을 JobQueue에서 제거한다.

    command가 지정되면 해당 명령만, None이면 전체 제거.
    """
    prefixes = []
    if command:
        prefixes.append(f"{command}_{journalist_id}_")
    else:
        prefixes.append(f"check_{journalist_id}_")
        prefixes.append(f"report_{journalist_id}_")

    for job in app.job_queue.jobs():
        if job.name and any(job.name.startswith(p) for p in prefixes):
            job.schedule_removal()


async def restore_schedules(app: Application, db) -> None:
    """서버 시작 시 DB의 스케줄을 JobQueue에 복원한다."""
    schedules = await repo.get_all_schedules(db)
    for s in schedules:
        register_job(
            app,
            command=s["command"],
            journalist_id=s["journalist_id"],
            telegram_id=s["telegram_id"],
            time_kst_str=s["time_kst"],
        )
    if schedules:
        logger.info("스케줄 복원 완료: %d건", len(schedules))
