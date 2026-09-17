from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from app.adapters.base import BaseAdapter, parse_dt
from app.schemas import SourceItem


class ArxivAdapter(BaseAdapter):
    source_name = "arxiv"
    categories = ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "stat.ML"]
    keywords = ["llm", "large language model", "agent", "rag", "multimodal", "reasoning", "tool use", "code generation", "video generation"]

    async def fetch(self) -> list[SourceItem]:
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        items: list[SourceItem] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for category in self.categories:
                response = await client.get(
                    "https://export.arxiv.org/api/query",
                    params={"search_query": f"cat:{category}", "sortBy": "submittedDate", "sortOrder": "descending", "start": 0, "max_results": 50},
                )
                if response.status_code == 429:
                    raise RuntimeError("arxiv rate limited: HTTP 429")
                response.raise_for_status()
                root = ET.fromstring(response.text)
                for entry in root.findall("atom:entry", ns):
                    title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
                    summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
                    if not any(k in f"{title} {summary}".lower() for k in self.keywords):
                        continue
                    paper_url = entry.findtext("atom:id", default="", namespaces=ns) or ""
                    paper_id = paper_url.rsplit("/", 1)[-1]
                    if not paper_id or paper_id in seen:
                        continue
                    seen.add(paper_id)
                    authors = [a.findtext("atom:name", default="", namespaces=ns) for a in entry.findall("atom:author", ns)]
                    primary = entry.find("arxiv:primary_category", ns)
                    items.append(SourceItem(
                        source=self.source_name,
                        source_item_id=paper_id,
                        title=title,
                        url=paper_url or f"https://arxiv.org/abs/{paper_id}",
                        author=", ".join([a for a in authors if a])[:500] or None,
                        summary=summary[:1000],
                        raw_text=f"{title} {summary}",
                        published_at=parse_dt(entry.findtext("atom:published", default="", namespaces=ns)),
                        metrics={"category": primary.attrib.get("term") if primary is not None else category},
                        raw_payload={"arxiv_id": paper_id, "category": category},
                    ))
                    if len(items) >= self.settings.max_items_per_source:
                        return items
                await self.polite_sleep(1.0)
        return items
