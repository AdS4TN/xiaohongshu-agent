from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.services.blog_export import export_blog_hotspots
from app.services.hotspot import run_full_ingest


async def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = await run_full_ingest(db)
        print(result)
        export_result = export_blog_hotspots(db)
        print(export_result)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
