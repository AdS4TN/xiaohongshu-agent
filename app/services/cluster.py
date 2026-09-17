from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import HotspotModel, HotspotSourceModel, SourceItemModel
from app.services.dedupe import cluster_key_for_item
from app.services.llm import enrich_with_llm
from app.services.scoring import infer_content_type, score_item


def _item_to_light(item: SourceItemModel):
    class Obj:
        pass

    obj = Obj()
    obj.source = item.source
    obj.source_item_id = item.source_item_id
    obj.title = item.title
    obj.url = item.url
    try:
        obj.metrics = json.loads(item.metrics_json or "{}")
        obj.raw_payload = json.loads(item.raw_payload_json or "{}")
    except json.JSONDecodeError:
        obj.metrics = {}
        obj.raw_payload = {}
    return obj


async def generate_hotspots(db: Session, hours: int = 72) -> int:
    settings = get_settings()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(
        select(SourceItemModel)
        .where(SourceItemModel.created_at >= since)
        .options(selectinload(SourceItemModel.hotspots))
        .order_by(SourceItemModel.created_at.desc())
        .limit(600)
    ).scalars().all()
    groups: dict[str, list[SourceItemModel]] = defaultdict(list)
    for item in rows:
        groups[cluster_key_for_item(_item_to_light(item))].append(item)

    candidates: list[tuple[float, list[SourceItemModel]]] = []
    for items in groups.values():
        if all(item.hotspots for item in items):
            continue
        candidates.append((max(score_item(i) for i in items) + min(len(items) - 1, 3) * 4, items))
    candidates.sort(key=lambda x: x[0], reverse=True)

    created = 0
    for idx, (base_score, items) in enumerate(candidates[:100]):
        representative = max(items, key=score_item)
        if idx < settings.max_llm_candidates_per_run:
            enriched = await enrich_with_llm(representative, base_score)
        else:
            from app.services.llm import fallback_summary

            enriched = fallback_summary(representative, base_score)
        hotspot = HotspotModel(
            title=representative.title,
            summary=enriched.get("summary"),
            why_it_matters=enriched.get("why_it_matters"),
            xiaohongshu_angle=enriched.get("xiaohongshu_angle"),
            content_type=enriched.get("content_type") or infer_content_type(representative),
            score=float(enriched.get("score") or base_score),
            status="new",
            risk=enriched.get("risk"),
        )
        db.add(hotspot)
        db.flush()
        for item in items:
            db.add(HotspotSourceModel(hotspot_id=hotspot.id, source_item_id=item.id))
        created += 1
    db.commit()
    return created
