from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models import HotspotModel, HotspotSourceModel
from app.services.llm import enrich_with_llm


async def main(limit: int = 30, fallback_only: bool = False) -> None:
    init_db()
    db = SessionLocal()
    updated = 0
    try:
        hotspots = db.execute(
            select(HotspotModel)
            .options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
            .order_by(HotspotModel.score.desc(), HotspotModel.created_at.desc())
            .limit(limit)
        ).scalars().all()

        for hotspot in hotspots:
            if not hotspot.sources:
                continue
            representative = hotspot.sources[0].source_item
            if fallback_only:
                from app.services.llm import fallback_summary

                enriched = fallback_summary(representative, hotspot.score)
            else:
                enriched = await enrich_with_llm(representative, hotspot.score)
            hotspot.summary = enriched.get("summary")
            hotspot.why_it_matters = enriched.get("why_it_matters")
            hotspot.xiaohongshu_angle = enriched.get("xiaohongshu_angle")
            hotspot.content_type = enriched.get("content_type") or hotspot.content_type
            hotspot.score = float(enriched.get("score") or hotspot.score)
            hotspot.risk = enriched.get("risk")
            updated += 1
            print(json.dumps({"id": hotspot.id, "title": hotspot.title, "updated": True}, ensure_ascii=False))
        db.commit()
    finally:
        db.close()
    print(f"updated={updated}")


if __name__ == "__main__":
    arg_limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    arg_fallback_only = "--fallback-only" in sys.argv or os.getenv("FALLBACK_ONLY") == "true"
    asyncio.run(main(arg_limit, fallback_only=arg_fallback_only))
