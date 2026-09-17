from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.adapters.base import parse_dt
from app.models import GitHubRepoSnapshotModel, SourceItemModel, SourceRunModel
from app.schemas import SourceItem
from app.services.dedupe import content_hash
from app.services.news_notify import notify_news_dashboard

logger = logging.getLogger(__name__)


def _augment_github_deltas(db: Session, item: SourceItem, now: datetime) -> None:
    """基于历史快照计算 GitHub Star 增长，首日没有历史时保持为空。"""
    if item.source != "github":
        return
    current_stars = int(item.metrics.get("stars") or 0)
    for hours in (24, 72):
        cutoff = now - timedelta(hours=hours)
        previous = db.execute(
            select(GitHubRepoSnapshotModel)
            .where(
                (GitHubRepoSnapshotModel.repo_full_name == item.source_item_id)
                & (GitHubRepoSnapshotModel.snapshot_time <= cutoff)
            )
            .order_by(desc(GitHubRepoSnapshotModel.snapshot_time))
            .limit(1)
        ).scalar_one_or_none()
        if previous is not None:
            item.metrics[f"star_delta_{hours}h"] = max(0, current_stars - previous.stars)


def _add_github_snapshot(db: Session, item: SourceItem, now: datetime) -> None:
    """为每次 GitHub 采集都记录 Star 快照，而不是只在首次入库时记录。"""
    if item.source != "github":
        return
    db.add(GitHubRepoSnapshotModel(
        repo_full_name=item.source_item_id,
        stars=int(item.metrics.get("stars") or 0),
        forks=int(item.metrics.get("forks") or 0),
        open_issues=item.metrics.get("open_issues"),
        pushed_at=parse_dt(item.metrics.get("pushed_at")),
        snapshot_time=now,
    ))


def insert_source_items(db: Session, items: list[SourceItem]) -> int:
    inserted = 0
    seen_hashes: set[str] = set()
    for item in items:
        now = datetime.now(timezone.utc)
        _augment_github_deltas(db, item, now)
        item_hash = content_hash(item)
        _add_github_snapshot(db, item, now)

        existing = db.execute(
            select(SourceItemModel).where(
                (SourceItemModel.source == item.source) & (SourceItemModel.source_item_id == item.source_item_id)
            )
        ).scalar_one_or_none()
        if existing:
            # 采集到同一来源条目时刷新指标，便于热点页看到最新 star/likes 等信息。
            existing.title = item.title
            existing.url = item.url
            existing.author = item.author
            existing.summary = item.summary
            existing.raw_text = item.raw_text
            existing.published_at = item.published_at
            existing.metrics_json = json.dumps(item.metrics, ensure_ascii=False)
            existing.raw_payload_json = json.dumps(item.raw_payload, ensure_ascii=False, default=str)
            continue

        if item_hash in seen_hashes:
            continue
        seen_hashes.add(item_hash)

        hash_exists = db.execute(
            select(SourceItemModel.id).where(SourceItemModel.content_hash == item_hash)
        ).scalar_one_or_none()
        if hash_exists:
            continue

        model = SourceItemModel(
            source=item.source,
            source_item_id=item.source_item_id,
            title=item.title,
            url=item.url,
            author=item.author,
            summary=item.summary,
            raw_text=item.raw_text,
            published_at=item.published_at,
            metrics_json=json.dumps(item.metrics, ensure_ascii=False),
            raw_payload_json=json.dumps(item.raw_payload, ensure_ascii=False, default=str),
            content_hash=item_hash,
        )
        db.add(model)
        inserted += 1
    db.commit()

    github_daily_items = [item for item in items if item.source == "github_trending_daily"]
    if github_daily_items:
        snapshot_times = sorted({str(item.metrics.get("snapshot_time") or "") for item in github_daily_items if item.metrics.get("snapshot_time")}, reverse=True)
        notify_news_dashboard(
            "github_trending_daily_ingested",
            {
                "items_fetched": len(github_daily_items),
                "items_inserted": inserted,
                "snapshot_time": snapshot_times[0] if snapshot_times else None,
                "repos": [item.source_item_id for item in github_daily_items[:25]],
            },
        )
    return inserted


async def run_adapter(db: Session, adapter) -> tuple[int, int]:
    started = datetime.now(timezone.utc)
    run = SourceRunModel(source=adapter.source_name, status="failed", started_at=started, items_fetched=0, items_inserted=0)
    db.add(run)
    db.commit()
    try:
        items = await adapter.fetch()
        inserted = insert_source_items(db, items)
        run.status = "success"
        run.items_fetched = len(items)
        run.items_inserted = inserted
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("来源 %s 抓取 %s 条，入库 %s 条", adapter.source_name, len(items), inserted)
        return len(items), inserted
    except Exception as exc:
        logger.exception("来源 %s 采集失败", adapter.source_name)
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return 0, 0
