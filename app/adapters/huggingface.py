from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.adapters.base import BaseAdapter, parse_dt
from app.schemas import SourceItem

AI_TAGS = ("llm", "text-generation", "image", "video", "audio", "agent", "rag", "multimodal", "diffusion", "chat")


class HuggingFaceBaseAdapter(BaseAdapter):
    def hf_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.hf_token:
            headers["Authorization"] = f"Bearer {self.settings.hf_token}"
        return headers


class HFDailyPapersAdapter(HuggingFaceBaseAdapter):
    source_name = "hf_daily_papers"

    async def fetch(self) -> list[SourceItem]:
        items: list[SourceItem] = []
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.hf_headers()) as client:
            for day in (date.today(), date.today() - timedelta(days=1)):
                data = await self._get_json(client, "https://huggingface.co/api/daily_papers", params={"date": day.isoformat()})
                papers = data if isinstance(data, list) else data.get("papers", []) if isinstance(data, dict) else []
                for paper in papers[:50]:
                    nested = paper.get("paper") if isinstance(paper.get("paper"), dict) else {}
                    pid = str(nested.get("id") or paper.get("id") or paper.get("arxivId") or paper.get("title"))
                    title = paper.get("title") or nested.get("title") or pid
                    url = paper.get("url") or nested.get("url") or f"https://huggingface.co/papers/{pid}"
                    items.append(SourceItem(
                        source=self.source_name,
                        source_item_id=pid,
                        title=title,
                        url=url,
                        author=paper.get("authors") if isinstance(paper.get("authors"), str) else None,
                        summary=paper.get("summary") or paper.get("abstract") or nested.get("summary"),
                        raw_text=f"{title} {paper.get('summary') or paper.get('abstract') or ''}",
                        published_at=parse_dt(paper.get("publishedAt") or paper.get("createdAt") or day.isoformat()),
                        metrics={"upvotes": paper.get("upvotes") or paper.get("votes") or 0, "hf_day": day.isoformat()},
                        raw_payload=paper,
                    ))
                    if len(items) >= self.settings.max_items_per_source:
                        return items
                await self.polite_sleep(0.4)
        return items


class HFModelsAdapter(HuggingFaceBaseAdapter):
    source_name = "hf_models"

    async def fetch(self) -> list[SourceItem]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.hf_headers()) as client:
            data = await self._get_json(client, "https://huggingface.co/api/models", params={"sort": "likes", "direction": -1, "limit": 100, "full": "true"})
        items: list[SourceItem] = []
        for model in data if isinstance(data, list) else []:
            model_id = model.get("modelId") or model.get("id")
            tags = model.get("tags") or []
            pipeline = model.get("pipeline_tag") or ""
            searchable = " ".join([str(model_id), pipeline, " ".join(map(str, tags))]).lower()
            if not model_id or not any(tag in searchable for tag in AI_TAGS):
                continue
            items.append(SourceItem(
                source=self.source_name,
                source_item_id=model_id,
                title=model_id,
                url=f"https://huggingface.co/{model_id}",
                author=model.get("author"),
                summary=f"HuggingFace 模型：{model_id}，任务类型 {pipeline or '未知'}。",
                raw_text=searchable,
                published_at=parse_dt(model.get("lastModified") or model.get("createdAt")),
                metrics={"likes": model.get("likes") or 0, "downloads": model.get("downloads") or 0, "tags": tags, "pipeline_tag": pipeline},
                raw_payload=model,
            ))
            if len(items) >= self.settings.max_items_per_source:
                return items
        return items


class HFSpacesAdapter(HuggingFaceBaseAdapter):
    source_name = "hf_spaces"

    async def fetch(self) -> list[SourceItem]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.hf_headers()) as client:
            data = await self._get_json(client, "https://huggingface.co/api/spaces", params={"sort": "likes", "direction": -1, "limit": 100, "full": "true"})
        items: list[SourceItem] = []
        for space in data if isinstance(data, list) else []:
            space_id = space.get("id") or space.get("name")
            tags = space.get("tags") or []
            sdk = space.get("sdk") or ""
            searchable = " ".join([str(space_id), sdk, " ".join(map(str, tags))]).lower()
            if not space_id or not any(tag in searchable for tag in AI_TAGS + ("gradio", "streamlit")):
                continue
            items.append(SourceItem(
                source=self.source_name,
                source_item_id=space_id,
                title=space_id,
                url=f"https://huggingface.co/spaces/{space_id}",
                author=space.get("author"),
                summary=f"HuggingFace Space Demo：{space_id}，SDK：{sdk or '未知'}。",
                raw_text=searchable,
                published_at=parse_dt(space.get("lastModified") or space.get("createdAt")),
                metrics={"likes": space.get("likes") or 0, "sdk": sdk, "tags": tags},
                raw_payload=space,
            ))
            if len(items) >= self.settings.max_items_per_source:
                return items
        return items
