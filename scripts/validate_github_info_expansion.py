from __future__ import annotations

import argparse
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
from app.services.github_info_expansion import expand_and_save_github_project_info


def _compact_summary(expansion: dict) -> dict:
    final_summary = expansion.get("final_summary") if isinstance(expansion, dict) else {}
    community = expansion.get("community_signals_context") if isinstance(expansion, dict) else {}
    platforms = community.get("platforms") if isinstance(community, dict) else {}
    return {
        "expanded_at": expansion.get("expanded_at"),
        "models": expansion.get("models"),
        "project": expansion.get("project"),
        "round1_verdict": (expansion.get("round1_review") or {}).get("verdict"),
        "round2_verdict": (expansion.get("round2_review") or {}).get("verdict"),
        "final_one_liner": final_summary.get("one_liner") if isinstance(final_summary, dict) else None,
        "community_signal_count": len(community.get("signals") or []) if isinstance(community, dict) else 0,
        "community_platforms": {
            key: value.get("summary")
            for key, value in platforms.items()
            if isinstance(value, dict)
        } if isinstance(platforms, dict) else {},
        "source_count_round1": len((expansion.get("round1_search") or {}).get("sources") or []),
        "source_count_round2": len((expansion.get("round2_search") or {}).get("sources") or []),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="验证 GitHub 项目信息扩展流水线。")
    parser.add_argument("--item-id", type=int, default=None, help="source_items.id；不传则取最新 GitHub Trending 第一条")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.item_id is None:
            item = db.execute(
                select(SourceItemModel)
                .where(SourceItemModel.source == "github_trending_daily")
                .order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if item is None:
                raise SystemExit("没有 github_trending_daily 数据，请先采集。")
            item_id = item.id
        else:
            item_id = args.item_id

        expansion = await expand_and_save_github_project_info(db, item_id)
        print(json.dumps(_compact_summary(expansion), ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
