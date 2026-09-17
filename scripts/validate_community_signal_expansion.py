from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models import SourceItemModel
from app.services.github_info_expansion import build_project_context
from app.services.community_signal_expansion import expand_community_signals


def _compact(result: dict) -> dict:
    platforms = result.get("platforms") if isinstance(result, dict) else {}
    return {
        "version": result.get("version"),
        "expanded_at": result.get("expanded_at"),
        "project": result.get("project"),
        "signal_count": len(result.get("signals") or []),
        "platforms": {
            key: value.get("summary")
            for key, value in platforms.items()
            if isinstance(value, dict)
        } if isinstance(platforms, dict) else {},
        "signals": [
            {
                "platform": signal.get("platform"),
                "source_type": signal.get("source_type"),
                "title": signal.get("title"),
                "url": signal.get("url"),
                "relevance_reason": signal.get("relevance_reason"),
            }
            for signal in (result.get("signals") or [])[:12]
        ],
        "limitations": result.get("limitations"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="验证 GitHub 项目的社区/传播信号爬虫。")
    parser.add_argument("--item-id", type=int, default=None, help="source_items.id")
    parser.add_argument("--repo-url", default=None, help="GitHub 仓库 URL；传了则不读数据库")
    parser.add_argument("--project-name", default=None, help="项目名/owner-repo")
    parser.add_argument("--alias", action="append", default=[], help="可重复传入项目别名")
    parser.add_argument("--max-results-per-platform", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    if args.repo_url:
        project = {
            "title": args.project_name or args.repo_url.rstrip("/").split("/")[-1],
            "url": args.repo_url,
            "aliases": args.alias,
        }
    else:
        if args.item_id is None:
            raise SystemExit("请传 --item-id 或 --repo-url")
        init_db()
        db = SessionLocal()
        try:
            item = db.execute(select(SourceItemModel).where(SourceItemModel.id == args.item_id)).scalar_one_or_none()
            if item is None:
                raise SystemExit(f"source item not found: {args.item_id}")
            project = build_project_context(item)
            if args.alias:
                project["aliases"] = args.alias
        finally:
            db.close()

    result = await expand_community_signals(
        project,
        max_results_per_platform=args.max_results_per_platform,
        timeout_seconds=args.timeout_seconds,
        include_fallback_signals=True,
    )
    print(json.dumps(_compact(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
