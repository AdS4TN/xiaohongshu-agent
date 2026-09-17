from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.services.blog_export import export_blog_hotspots, export_blog_hotspots_with_llm


def main() -> None:
    parser = argparse.ArgumentParser(description="导出个人博客 AI/科技热点 JSON")
    parser.add_argument("--date", dest="target_date", help="按指定日期导出，格式 YYYY-MM-DD；默认导出最新一期")
    parser.add_argument("--limit", type=int, default=None, help="导出卡片数量，默认读取 DAILY_CARD_LIMIT")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 Codex 写作，直接用规则兜底导出")
    parser.add_argument("--force-analysis", action="store_true", help="强制重新调用 Codex 生成 A/B 层缓存")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        target = date.fromisoformat(args.target_date) if args.target_date else None
        if args.skip_llm:
            result = export_blog_hotspots(db, target_date=target, limit=args.limit)
        else:
            result = asyncio.run(
                export_blog_hotspots_with_llm(db, target_date=target, limit=args.limit, force_analysis=args.force_analysis)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
