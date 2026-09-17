from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import SourceRunModel
from app.services.daily_report import ensure_daily_report
from app.services.blog_export import export_blog_hotspots_with_llm
from app.services.hotspot import is_running, run_full_ingest

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()
MISFIRE_GRACE_SECONDS = 12 * 60 * 60


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _today_start_utc(timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    now_local = datetime.now(tz)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc)


def _has_successful_github_trending_run_today() -> bool:
    """判断今天本地时区内是否已经成功抓过 GitHub Trending Daily。"""

    settings = get_settings()
    since_utc = _today_start_utc(settings.timezone)
    db = SessionLocal()
    try:
        return (
            db.execute(
                select(SourceRunModel.id)
                .where(
                    SourceRunModel.source == "github_trending_daily",
                    SourceRunModel.status == "success",
                    SourceRunModel.started_at >= since_utc,
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )
    finally:
        db.close()


def _should_schedule_startup_catchup() -> bool:
    """服务启动/恢复时，如果已经过了早间更新时间但今天没跑过，则补跑一次。"""

    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz)
    daily_hour, daily_minute = _parse_time(settings.daily_run_time)
    daily_local = datetime.combine(now_local.date(), time(hour=daily_hour, minute=daily_minute), tzinfo=tz)
    if now_local < daily_local:
        return False
    return not _has_successful_github_trending_run_today()


async def scheduled_run() -> None:
    if is_running():
        logger.info("采集任务已在运行，本次调度跳过")
        return
    db = SessionLocal()
    try:
        await run_full_ingest(db)
        await export_blog_hotspots_with_llm(db)
        await ensure_daily_report(db)
    finally:
        db.close()


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.enable_scheduler or scheduler.running:
        return
    for name, value in {"daily": settings.daily_run_time, "evening": settings.evening_run_time}.items():
        hour, minute = _parse_time(value)
        scheduler.add_job(
            scheduled_run,
            CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            id=f"ai_radar_{name}",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            coalesce=True,
        )
    scheduler.start()
    logger.info("APScheduler 已启动：%s / %s", settings.daily_run_time, settings.evening_run_time)
    if _should_schedule_startup_catchup():
        scheduler.add_job(
            scheduled_run,
            "date",
            run_date=datetime.now(ZoneInfo(settings.timezone)) + timedelta(seconds=3),
            id="ai_radar_startup_catchup",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60 * 60,
        )
        logger.info("今天尚未成功采集 GitHub Trending，已安排启动补跑任务")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
