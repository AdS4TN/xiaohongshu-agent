from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.community_signal_expansion import collect_reddit_community_signals


async def main() -> None:
    result = await collect_reddit_community_signals(
        project_name="pbakaus/impeccable",
        repo_url="https://github.com/pbakaus/impeccable",
        aliases=["impeccable"],
        max_results=3,
        timeout_seconds=20,
        include_fallback_signal=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
