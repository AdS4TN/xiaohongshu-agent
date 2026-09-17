from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    source: str
    source_item_id: str
    title: str
    url: str
    author: str | None = None
    summary: str | None = None
    raw_text: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class SourceLink(BaseModel):
    source: str
    title: str
    url: str
    author: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class HotspotOut(BaseModel):
    id: int
    title: str
    summary: str | None = None
    why_it_matters: str | None = None
    xiaohongshu_angle: str | None = None
    content_type: str
    score: float
    status: str
    risk: str | None = None
    created_at: datetime
    updated_at: datetime
    sources: list[SourceLink] = Field(default_factory=list)


class SelectionIn(BaseModel):
    note: str | None = None
