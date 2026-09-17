from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from app.adapters.base import BaseAdapter
from app.schemas import SourceItem


GITHUB_BASE_URL = "https://github.com"
TRENDING_URL = f"{GITHUB_BASE_URL}/trending?since=daily"
SOURCE_NAME = "github_trending_daily"

_STARS_TODAY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?\s*[kKmM]?)\s+stars?\s+today", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s*([kKmM]))?")


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _parse_compact_int(value: str | None) -> int:
    text = _normalize_text(value)
    match = _NUMBER_RE.search(text)
    if not match:
        return 0

    number_text, suffix = match.groups()
    try:
        number = float(number_text.replace(",", ""))
    except ValueError:
        return 0

    if suffix:
        suffix = suffix.lower()
        if suffix == "k":
            number *= 1_000
        elif suffix == "m":
            number *= 1_000_000
    return int(number)


def _snapshot_iso(snapshot_time: datetime | None) -> str:
    if snapshot_time is None:
        snapshot_time = datetime.now(timezone.utc)
    elif snapshot_time.tzinfo is None:
        snapshot_time = snapshot_time.replace(tzinfo=timezone.utc)
    else:
        snapshot_time = snapshot_time.astimezone(timezone.utc)
    return snapshot_time.isoformat().replace("+00:00", "Z")


def _repo_full_name(link: Tag) -> str | None:
    href = str(link.get("href") or "")
    path = unquote(urlparse(urljoin(GITHUB_BASE_URL, href)).path).strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"

    text = _normalize_text(link.get_text(" ", strip=True))
    text = re.sub(r"\s*/\s*", "/", text).strip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _repo_metric_from_link(article: Tag, full_name: str, suffix: str) -> int:
    expected_path = f"/{full_name}/{suffix}"
    for link in article.select("a[href]"):
        href = str(link.get("href") or "")
        path = unquote(urlparse(urljoin(GITHUB_BASE_URL, href)).path)
        if path == expected_path:
            return _parse_compact_int(link.get_text(" ", strip=True))
    return 0


def _stars_today(article: Tag) -> int:
    text = article.get_text(" ", strip=True)
    match = _STARS_TODAY_RE.search(_normalize_text(text))
    if not match:
        return 0
    return _parse_compact_int(match.group(1))


def parse_trending_html(html: str, *, snapshot_time: datetime | None = None) -> list[SourceItem]:
    """解析 GitHub Trending 日榜 HTML，返回可离线测试的 SourceItem 列表。"""
    soup = BeautifulSoup(html, "html.parser")
    snapshot = _snapshot_iso(snapshot_time)
    items: list[SourceItem] = []

    for article in soup.select("article.Box-row"):
        if not isinstance(article, Tag):
            continue

        repo_link = article.select_one("h2 a[href]")
        if not isinstance(repo_link, Tag):
            continue

        full_name = _repo_full_name(repo_link)
        if not full_name:
            continue

        rank = len(items) + 1
        owner = full_name.split("/", 1)[0]
        description_node = article.select_one("p")
        description = _normalize_text(description_node.get_text(" ", strip=True) if description_node else "")
        language_node = article.select_one("[itemprop='programmingLanguage']")
        language = _normalize_text(language_node.get_text(" ", strip=True) if language_node else "")
        stars = _repo_metric_from_link(article, full_name, "stargazers")
        forks = _repo_metric_from_link(article, full_name, "forks")
        stars_today = _stars_today(article)
        repo_url = f"{GITHUB_BASE_URL}/{full_name}"

        metrics = {
            "rank": rank,
            "language": language,
            "stars": stars,
            "forks": forks,
            "stars_today": stars_today,
            "trending_since": "daily",
            "trending_url": TRENDING_URL,
            "snapshot_time": snapshot,
            "discovery_source": SOURCE_NAME,
        }
        raw_payload = {
            "rank": rank,
            "full_name": full_name,
            "description": description,
            "language": language,
            "stars_total": stars,
            "forks_total": forks,
            "stars_today": stars_today,
            "url": repo_url,
            "trending_url": TRENDING_URL,
        }
        raw_text_parts = [full_name, description, language]

        items.append(SourceItem(
            source=SOURCE_NAME,
            source_item_id=full_name,
            title=full_name,
            url=repo_url,
            author=owner,
            summary=description,
            raw_text=" ".join(part for part in raw_text_parts if part),
            published_at=None,
            metrics=metrics,
            raw_payload=raw_payload,
        ))

    if not items:
        raise RuntimeError("github_trending_daily parsed 0 repositories from Trending HTML")
    return items


class GitHubTrendingDailyAdapter(BaseAdapter):
    source_name = SOURCE_NAME

    async def fetch(self) -> list[SourceItem]:
        headers = {
            "User-Agent": "AI-Radar/1.0 (+https://github.com/trending)",
            "Accept": "text/html,application/xhtml+xml",
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(TRENDING_URL)
            if response.status_code == 429:
                raise RuntimeError("github_trending_daily rate limited: HTTP 429")
            response.raise_for_status()

        items = parse_trending_html(response.text)
        return items[: self.settings.max_items_per_source]
