from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.adapters.base import BaseAdapter
from app.schemas import SourceItem


class HackerNewsAdapter(BaseAdapter):
    source_name = "hackernews"
    feeds = ["topstories", "newstories", "showstories", "askstories"]
    keywords = ["ai", "llm", "agent", "openai", "claude", "gemini", "tool", "startup", "open source", "machine learning", "model"]

    async def fetch(self) -> list[SourceItem]:
        base = "https://hacker-news.firebaseio.com/v0"
        items: list[SourceItem] = []
        seen: set[int] = set()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for feed in self.feeds:
                ids = await self._get_json(client, f"{base}/{feed}.json")
                for item_id in (ids or [])[:50]:
                    if item_id in seen:
                        continue
                    seen.add(item_id)
                    item = await self._get_json(client, f"{base}/item/{item_id}.json")
                    title = item.get("title") or ""
                    text = item.get("text") or ""
                    if not any(k in f"{title} {text}".lower() for k in self.keywords):
                        continue
                    items.append(SourceItem(
                        source=self.source_name,
                        source_item_id=str(item_id),
                        title=title or f"HN item {item_id}",
                        url=item.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
                        author=item.get("by"),
                        summary=text,
                        raw_text=f"{title} {text}",
                        published_at=datetime.fromtimestamp(item.get("time"), tz=timezone.utc) if item.get("time") else None,
                        metrics={"score": item.get("score") or 0, "descendants": item.get("descendants") or 0, "feed": feed},
                        raw_payload=item,
                    ))
                    if len(items) >= self.settings.max_items_per_source:
                        return items
                    await self.polite_sleep(0.05)
        return items
