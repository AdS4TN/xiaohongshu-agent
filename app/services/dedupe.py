from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

from app.schemas import SourceItem


def normalize_title(title: str) -> str:
    text = re.sub(r"\W+", " ", title.lower()).strip()
    return re.sub(r"\s+", " ", text)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def content_hash(item: SourceItem) -> str:
    raw = "|".join([item.source, item.source_item_id, normalize_url(item.url), normalize_title(item.title)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cluster_key_for_item(item) -> str:
    """生成轻量聚类键：优先 repo / arxiv id / URL，最后回落到标题关键词。"""
    metrics = item.metrics if hasattr(item, "metrics") else {}
    raw_payload = item.raw_payload if hasattr(item, "raw_payload") else {}
    source = item.source
    source_item_id = item.source_item_id
    if source in {"github", "github_trending_daily"}:
        return f"github:{source_item_id.lower()}"
    if source in {"arxiv", "hf_daily_papers"}:
        possible = source_item_id.lower()
        match = re.search(r"\d{4}\.\d{4,5}(v\d+)?", possible)
        if match:
            return f"arxiv:{match.group(0).split('v')[0]}"
        url_match = re.search(r"\d{4}\.\d{4,5}(v\d+)?", item.url.lower())
        if url_match:
            return f"arxiv:{url_match.group(0).split('v')[0]}"
    parsed = normalize_url(item.url)
    if parsed:
        return f"url:{parsed}"
    words = [w for w in normalize_title(item.title).split() if len(w) > 3][:8]
    return "title:" + "-".join(words)
