from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import desc, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models import SourceItemModel
from app.services.github_evidence import enrich_github_source_items


def _safe_json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


async def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = await enrich_github_source_items(db, limit=3, hours=168, force=True)
        print(json.dumps({"enrich_result": result}, ensure_ascii=False, indent=2))

        items = db.execute(
            select(SourceItemModel)
            .where(SourceItemModel.source.in_(("github_trending_daily", "github")))
            .order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at))
            .limit(3)
        ).scalars().all()

        rows = []
        for item in items:
            metrics = _safe_json_loads(item.metrics_json, {})
            raw_payload = _safe_json_loads(item.raw_payload_json, {})
            readme_text = raw_payload.get("readme_text") or raw_payload.get("readme_excerpt") or ""
            rows.append(
                {
                    "source": item.source,
                    "source_item_id": item.source_item_id,
                    "repo_created_at": metrics.get("repo_created_at"),
                    "repo_pushed_at": metrics.get("repo_pushed_at"),
                    "topics": metrics.get("topics"),
                    "has_readme": metrics.get("has_readme"),
                    "readme_chars": metrics.get("readme_chars"),
                    "readme_saved_chars": len(readme_text),
                    "raw_text_len": len(item.raw_text or ""),
                }
            )

        print(json.dumps({"items": rows}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
