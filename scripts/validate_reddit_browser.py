from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.community_signal_expansion import (
    _reddit_browser_profile_dir,
    collect_reddit_community_signals,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="验证 Reddit 浏览器登录态采集是否可用。")
    parser.add_argument("--project-name", default="pbakaus/impeccable")
    parser.add_argument("--repo-url", default="https://github.com/pbakaus/impeccable")
    parser.add_argument("--alias", action="append", default=["impeccable"])
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=25.0)
    args = parser.parse_args()

    profile_dir = _reddit_browser_profile_dir()
    print(
        json.dumps(
            {
                "profile_dir": str(profile_dir) if profile_dir else None,
                "profile_exists": bool(profile_dir and profile_dir.exists()),
                "project_name": args.project_name,
                "repo_url": args.repo_url,
                "aliases": args.alias,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    result = await collect_reddit_community_signals(
        project_name=args.project_name,
        repo_url=args.repo_url,
        aliases=args.alias,
        max_results=args.max_results,
        timeout_seconds=args.timeout_seconds,
        include_fallback_signal=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
