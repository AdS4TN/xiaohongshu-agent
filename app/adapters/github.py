from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.adapters.base import BaseAdapter, parse_dt
from app.schemas import SourceItem


class GitHubAdapter(BaseAdapter):
    source_name = "github"
    keywords = ["llm", "ai-agent", "agent", "rag", "mcp", "generative-ai", "ai-tools", "chatgpt"]

    async def fetch(self) -> list[SourceItem]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        items: list[SourceItem] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            for keyword in self.keywords:
                data = await self._get_json(
                    client,
                    "https://api.github.com/search/repositories",
                    params={"q": f"topic:{keyword} pushed:>{since}", "sort": "stars", "order": "desc", "per_page": 15},
                )
                for repo in data.get("items", []):
                    full_name = repo.get("full_name")
                    if not full_name or full_name in seen:
                        continue
                    seen.add(full_name)
                    topics = repo.get("topics") or []
                    description = repo.get("description") or ""
                    items.append(SourceItem(
                        source=self.source_name,
                        source_item_id=full_name,
                        title=full_name,
                        url=repo.get("html_url") or f"https://github.com/{full_name}",
                        author=(repo.get("owner") or {}).get("login"),
                        summary=description,
                        raw_text=" ".join([full_name, description, " ".join(topics)]),
                        published_at=parse_dt(repo.get("created_at")),
                        metrics={
                            "stars": repo.get("stargazers_count") or 0,
                            "forks": repo.get("forks_count") or 0,
                            "open_issues": repo.get("open_issues_count"),
                            "language": repo.get("language"),
                            "topics": topics,
                            "homepage": repo.get("homepage"),
                            "pushed_at": repo.get("pushed_at"),
                        },
                        raw_payload=repo,
                    ))
                    if len(items) >= self.settings.max_items_per_source:
                        return items
                await self.polite_sleep(0.4)
        return items
