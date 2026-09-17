from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.adapters import ArxivAdapter, GitHubAdapter, GitHubTrendingDailyAdapter, HackerNewsAdapter, HFDailyPapersAdapter, HFModelsAdapter, HFSpacesAdapter
from app.models import HotspotModel, HotspotSourceModel, SourceRunModel, UserSelectionModel
from app.schemas import HotspotOut, SourceLink
from app.services.cluster import generate_hotspots
from app.services.github_evidence import enrich_github_source_items
from app.services.ingest import run_adapter

logger = logging.getLogger(__name__)
_run_lock = asyncio.Lock()


def adapters():
    return [GitHubTrendingDailyAdapter(), GitHubAdapter(), HFDailyPapersAdapter(), ArxivAdapter(), HFModelsAdapter(), HFSpacesAdapter(), HackerNewsAdapter()]


async def run_full_ingest(db: Session) -> dict:
    if _run_lock.locked():
        return {"status": "already_running"}
    async with _run_lock:
        fetched = inserted = 0
        for adapter in adapters():
            f, i = await run_adapter(db, adapter)
            fetched += f
            inserted += i
        github_evidence = await enrich_github_source_items(db, limit=50, hours=72)
        created = await generate_hotspots(db)
        logger.info("完整采集完成：抓取 %s，入库 %s，GitHub 证据 %s，生成热点 %s", fetched, inserted, github_evidence, created)
        return {
            "status": "finished",
            "items_fetched": fetched,
            "items_inserted": inserted,
            "github_evidence": github_evidence,
            "hotspots_created": created,
        }


def hotspot_to_schema(h: HotspotModel) -> HotspotOut:
    links: list[SourceLink] = []
    for hs in h.sources:
        item = hs.source_item
        try:
            metrics = json.loads(item.metrics_json or "{}")
        except json.JSONDecodeError:
            metrics = {}
        links.append(SourceLink(
            source=item.source,
            title=item.title,
            url=item.url,
            author=item.author,
            published_at=item.published_at,
            metrics=metrics,
        ))
    return HotspotOut(
        id=h.id,
        title=h.title,
        summary=h.summary,
        why_it_matters=h.why_it_matters,
        xiaohongshu_angle=h.xiaohongshu_angle,
        content_type=h.content_type,
        score=h.score,
        status=h.status,
        risk=h.risk,
        created_at=h.created_at,
        updated_at=h.updated_at,
        sources=links,
    )


def list_hotspots(db: Session, status: str | None = None, hours: int | None = 48) -> list[HotspotOut]:
    stmt = select(HotspotModel).options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
    if status:
        stmt = stmt.where(HotspotModel.status == status)
    elif hours:
        stmt = stmt.where(HotspotModel.created_at >= datetime.now(timezone.utc) - timedelta(hours=hours))
    stmt = stmt.order_by(desc(HotspotModel.score), desc(HotspotModel.created_at)).limit(200)
    return [hotspot_to_schema(h) for h in db.execute(stmt).scalars().all()]


def set_hotspot_status(db: Session, hotspot_id: int, selection_type: str, note: str | None = None) -> HotspotOut | None:
    mapping = {"select": "selected", "ignore": "ignored", "watch": "watching"}
    status = mapping.get(selection_type, selection_type)
    hotspot = db.get(HotspotModel, hotspot_id)
    if not hotspot:
        return None
    hotspot.status = status
    db.add(UserSelectionModel(hotspot_id=hotspot.id, selection_type=status, note=note))
    db.commit()
    db.refresh(hotspot)
    hotspot = db.execute(
        select(HotspotModel).where(HotspotModel.id == hotspot_id).options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
    ).scalar_one()
    return hotspot_to_schema(hotspot)


def latest_source_runs(db: Session) -> list[SourceRunModel]:
    sources = [a.source_name for a in adapters()]
    result = []
    for source in sources:
        run = db.execute(select(SourceRunModel).where(SourceRunModel.source == source).order_by(desc(SourceRunModel.started_at)).limit(1)).scalar_one_or_none()
        if run:
            result.append(run)
        else:
            result.append(SourceRunModel(source=source, status="never", started_at=datetime.now(timezone.utc), items_fetched=0, items_inserted=0))
    return result


def is_running() -> bool:
    return _run_lock.locked()
