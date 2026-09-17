from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import SourceItem


class BaseAdapter:
    source_name: str = "base"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.timeout = httpx.Timeout(self.settings.request_timeout_seconds)

    async def fetch(self) -> list[SourceItem]:
        raise NotImplementedError

    async def _get_json(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
        response = await client.get(url, **kwargs)
        if response.status_code == 429:
            raise RuntimeError(f"{self.source_name} rate limited: HTTP 429")
        response.raise_for_status()
        return response.json()

    async def polite_sleep(self, seconds: float = 0.2) -> None:
        await asyncio.sleep(seconds)


def parse_dt(value: Any):
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return parsedate_to_datetime(str(value))
        except Exception:
            return None
