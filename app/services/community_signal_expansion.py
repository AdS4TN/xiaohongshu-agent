from __future__ import annotations

import asyncio
import logging
import re
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from app.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

COMMUNITY_SIGNAL_EXPANSION_VERSION = "community_signal_expansion_v1"
HACKER_NEWS_SIGNAL_EXPANSION_VERSION = "hacker_news_signal_expansion_v1"
REDDIT_SIGNAL_EXPANSION_VERSION = "reddit_signal_expansion_v2"
LINUX_DO_SIGNAL_EXPANSION_VERSION = "linux_do_signal_expansion_v1"
ALL_COMMUNITY_SIGNAL_EXPANSION_VERSION = "all_community_signal_expansion_v2"

_HN_ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
_REDDIT_SEARCH_JSON_URLS = (
    "https://www.reddit.com/search.json",
    "https://old.reddit.com/search.json",
)
_REDDIT_OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REDDIT_OAUTH_BASE_URL = "https://oauth.reddit.com"
_REDDIT_OLD_SEARCH_URL = "https://old.reddit.com/search/"
_DDG_HTML_URL = "https://duckduckgo.com/html/"
_LINUX_DO_BASE_URL = "https://linux.do"
_LINUX_DO_SEARCH_JSON_URL = f"{_LINUX_DO_BASE_URL}/search.json"
_LINUX_DO_LATEST_RSS_URL = f"{_LINUX_DO_BASE_URL}/latest.rss"
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; XiaohongshuAgent/1.0; "
    "+https://github.com/community-signal-expansion)"
)
_MAX_QUERY_TERMS = 8
_MAX_FETCH_CHARS = 900_000
_TEXT_SNIPPET_CHARS = 420
_HN_EXCERPT_CHARS = 420
_REDDIT_EXCERPT_CHARS = 420
_LINUX_DO_EXCERPT_CHARS = 420
_HN_DEFAULT_TAGS = ("story",)
_DEFAULT_SOURCE_TYPES_BY_HOST = {
    "producthunt.com": "product_hunt",
    "www.producthunt.com": "product_hunt",
    "substack.com": "newsletter",
    "medium.com": "technical_blog",
    "dev.to": "technical_blog",
    "hashnode.dev": "technical_blog",
    "hackernoon.com": "technical_blog",
    "thenewstack.io": "technical_article",
    "infoq.com": "technical_article",
}


@dataclass(frozen=True)
class GitHubProjectContext:
    """主服务可传入的 GitHub 项目上下文。

    - project_name：项目名，可以是 repo name，也可以是 owner/repo。
    - repo_url：GitHub 仓库 URL。
    - aliases：项目别名、包名、产品名、作者常用简称等。
    """

    project_name: str | None = None
    repo_url: str | None = None
    aliases: Sequence[str] | None = None


@dataclass(frozen=True)
class HackerNewsSearchQuery:
    """一次 Algolia HN Search API 查询。"""

    query: str
    tags: str
_NOISY_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "fbclid",
    "gclid",
}
_ENGAGEMENT_PATTERNS = {
    "upvotes": re.compile(r"\b([0-9][0-9,\.]*[kKmM]?)\s+(?:upvotes?|votes?)\b"),
    "comments": re.compile(r"\b([0-9][0-9,\.]*[kKmM]?)\s+comments?\b"),
    "likes": re.compile(r"\b([0-9][0-9,\.]*[kKmM]?)\s+likes?\b"),
    "shares": re.compile(r"\b([0-9][0-9,\.]*[kKmM]?)\s+shares?\b"),
    "reads": re.compile(r"\b([0-9][0-9,\.]*[kKmM]?)\s+(?:reads?|views?)\b"),
}


@dataclass(frozen=True)
class CommunitySignalCandidate:
    """搜索阶段得到的候选 URL，保留标题和摘要便于抓取失败时降级。"""

    url: str
    title: str | None = None
    snippet: str | None = None
    source_query: str | None = None


_reddit_oauth_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_reddit_oauth_lock = asyncio.Lock()


def _unknown_signal(url: str = "") -> dict[str, Any]:
    """返回统一 JSON 的空壳，确保下游字段稳定。"""

    return {
        "platform": "unknown",
        "url": url,
        "title": "unknown",
        "published_at": "unknown",
        "engagement": "unknown",
        "stance": "unknown",
        "quote_or_excerpt": "unknown",
        "source_type": "unknown",
        "topic_tags": [],
        "relevance_reason": "unknown",
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_text(value: str | None, *, max_chars: int | None = None) -> str:
    text = unescape(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _repo_owner_name(repo_url: str | None) -> tuple[str | None, str | None]:
    if not repo_url:
        return None, None
    parsed = urlparse(repo_url)
    if "github.com" not in parsed.netloc.lower():
        return None, None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1].removesuffix(".git")


def _normalize_aliases(
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> list[str]:
    owner, repo_name = _repo_owner_name(repo_url)
    raw_terms: list[str | None] = [project_name, repo_name, f"{owner}/{repo_name}" if owner and repo_name else None]
    raw_terms.extend(aliases or [])

    normalized: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        clean = _compact_text(term)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized


def _github_repo_url_variants(repo_url: str | None) -> list[str]:
    """生成 HN 中常见的 GitHub URL 写法，提升命中率。"""

    owner, repo_name = _repo_owner_name(repo_url)
    if not owner or not repo_name:
        return []
    return [
        f"{owner}/{repo_name}",
        f"github.com/{owner}/{repo_name}",
        f"https://github.com/{owner}/{repo_name}",
        f"http://github.com/{owner}/{repo_name}",
    ]


def _hn_search_terms(project_name: str | None, repo_url: str | None, aliases: Sequence[str] | None) -> list[str]:
    """从 GitHub 项目上下文提取适合 HN 搜索的关键词。

    Algolia HN Search 的 query 会同时匹配 story title/url 与 comment text。
    这里不做复杂布尔表达式，优先用短、准、可解释的关键词多次查询。
    """

    owner, repo_name = _repo_owner_name(repo_url)
    if owner and repo_name:
        terms = [
            f"{owner}/{repo_name}",
            f"github.com/{owner}/{repo_name}",
            f"https://github.com/{owner}/{repo_name}",
            f"http://github.com/{owner}/{repo_name}",
        ]
        for alias in aliases or []:
            clean_alias = _compact_text(alias)
            if "/" in clean_alias or "github.com/" in clean_alias.casefold() or len(clean_alias.split()) >= 2:
                terms.append(clean_alias)
        terms.append(repo_name)
    else:
        terms = [*_normalize_aliases(project_name, repo_url, aliases), *_github_repo_url_variants(repo_url)]
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = _compact_text(term)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped[:_MAX_QUERY_TERMS]


def build_hacker_news_search_queries(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    tags: Sequence[str] = _HN_DEFAULT_TAGS,
) -> list[HackerNewsSearchQuery]:
    """构建 Algolia HN Search API 查询计划。

    默认分别查 `story` 和 `comment`，因为 HN 里很多有价值反馈只出现在评论中。
    """

    terms = _hn_search_terms(project_name, repo_url, aliases)
    queries: list[HackerNewsSearchQuery] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        for tag in tags:
            clean_tag = _compact_text(tag)
            if not clean_tag:
                continue
            key = (term.casefold(), clean_tag.casefold())
            if key in seen:
                continue
            seen.add(key)
            queries.append(HackerNewsSearchQuery(query=term, tags=clean_tag))
    return queries


def _clean_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    query = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {
        key: values
        for key, values in query.items()
        if key.lower() not in _NOISY_QUERY_PARAMS and not key.lower().startswith("utm_")
    }
    clean_query = urlencode(filtered, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", clean_query, ""))


def _decode_duckduckgo_href(href: str | None) -> str:
    if not href:
        return ""
    href = unescape(href)
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return _clean_url(query["uddg"][0])
    if parsed.scheme in {"http", "https"}:
        return _clean_url(href)
    return ""


def _html_to_plain_text(value: str | None, *, max_chars: int | None = None) -> str:
    """把 HN/Algolia 返回的 HTML 片段转为可展示文本。"""

    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return _compact_text(soup.get_text(" ", strip=True), max_chars=max_chars)


def _hn_item_url(hit: Mapping[str, Any]) -> str:
    object_id = hit.get("objectID") or hit.get("story_id") or hit.get("id")
    if object_id is None:
        return "https://news.ycombinator.com/"
    return f"https://news.ycombinator.com/item?id={object_id}"


def _hn_source_type(hit: Mapping[str, Any]) -> str:
    tags = hit.get("_tags")
    if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)):
        tag_set = {str(tag).lower() for tag in tags}
        if "comment" in tag_set:
            return "comment"
        if "story" in tag_set:
            return "story"
    if hit.get("comment_text"):
        return "comment"
    return "story"


def _hn_title(hit: Mapping[str, Any]) -> str:
    title = hit.get("title") or hit.get("story_title")
    return _compact_text(str(title) if title else None) or "unknown"


def _hn_published_at(hit: Mapping[str, Any]) -> str:
    created_at = hit.get("created_at")
    if created_at:
        parsed = _parse_datetime_to_iso(str(created_at))
        return parsed or str(created_at)

    created_at_i = hit.get("created_at_i")
    if isinstance(created_at_i, (int, float)):
        return datetime.fromtimestamp(created_at_i, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return "unknown"


def _hn_engagement(hit: Mapping[str, Any]) -> dict[str, Any] | str:
    engagement: dict[str, Any] = {}
    for source_key, target_key in (
        ("points", "points"),
        ("num_comments", "comments"),
        ("story_id", "story_id"),
        ("parent_id", "parent_id"),
        ("author", "author"),
    ):
        value = hit.get(source_key)
        if value is not None:
            engagement[target_key] = value
    return engagement or "unknown"


def _hn_excerpt(hit: Mapping[str, Any]) -> str:
    source_type = _hn_source_type(hit)
    if source_type == "comment":
        text = _html_to_plain_text(str(hit.get("comment_text") or ""), max_chars=_HN_EXCERPT_CHARS)
        if text:
            return text

    story_text = _html_to_plain_text(str(hit.get("story_text") or ""), max_chars=_HN_EXCERPT_CHARS)
    if story_text:
        return story_text

    title = _hn_title(hit)
    url = hit.get("url") or hit.get("story_url")
    parts = [part for part in (title if title != "unknown" else "", str(url or "")) if part]
    return _compact_text(" | ".join(parts), max_chars=_HN_EXCERPT_CHARS) or "unknown"


def _hn_signal_url(hit: Mapping[str, Any]) -> str:
    """统一指向 HN item，保留讨论上下文；原始外链留在 engagement.external_url。"""

    return _hn_item_url(hit)


def _hn_topic_tags(hit: Mapping[str, Any], match_terms: Sequence[str]) -> list[str]:
    tags = ["hacker_news", _hn_source_type(hit)]
    raw_tags = hit.get("_tags")
    if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes)):
        for tag in raw_tags:
            clean = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(tag).strip().lower()).strip("_")
            if clean:
                tags.append(clean[:50])

    text = f"{_hn_title(hit)} {_hn_excerpt(hit)} {hit.get('url') or ''} {hit.get('story_url') or ''}".casefold()
    for keyword, tag in (
        ("github", "github"),
        ("open source", "open_source"),
        ("ai", "ai"),
        ("agent", "agent"),
        ("llm", "llm"),
        ("developer", "developer_tools"),
        ("launch", "launch"),
    ):
        if keyword in text:
            tags.append(tag)

    for term in match_terms[:5]:
        if term.casefold() not in text:
            continue
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", term.strip().lower()).strip("_")
        if safe:
            tags.append(safe[:40])

    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _hn_relevance_reason(
    hit: Mapping[str, Any],
    *,
    query: str,
    match_terms: Sequence[str],
    repo_url: str | None,
) -> str:
    haystack = " ".join(
        str(value or "")
        for value in (
            _hn_title(hit),
            _hn_excerpt(hit),
            hit.get("url"),
            hit.get("story_url"),
            hit.get("comment_text"),
            hit.get("story_text"),
        )
    ).casefold()
    matched = [term for term in match_terms if term.casefold() in haystack]
    if repo_url and repo_url.casefold() in haystack:
        matched.append(repo_url)
    if matched:
        return f"HN {_hn_source_type(hit)} 命中项目上下文关键词：{', '.join(dict.fromkeys(matched[:5]))}"
    return f"Algolia HN Search API 查询命中，query={query}；建议人工复核是否同名项目"


def _hn_hit_to_signal(
    hit: Mapping[str, Any],
    *,
    query: str,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> dict[str, Any]:
    match_terms = _hn_search_terms(project_name, repo_url, aliases)
    signal = _unknown_signal(_hn_signal_url(hit))
    engagement = _hn_engagement(hit)
    external_url = hit.get("url") or hit.get("story_url")
    if isinstance(engagement, dict) and external_url:
        engagement["external_url"] = str(external_url)
    signal.update(
        {
            "platform": "Hacker News",
            "url": _hn_signal_url(hit),
            "title": _hn_title(hit),
            "published_at": _hn_published_at(hit),
            "engagement": engagement,
            "stance": "unknown",
            "quote_or_excerpt": _hn_excerpt(hit),
            "source_type": _hn_source_type(hit),
            "topic_tags": _hn_topic_tags(hit, match_terms),
            "relevance_reason": _hn_relevance_reason(hit, query=query, match_terms=match_terms, repo_url=repo_url),
        }
    )
    return signal


def _hn_signal_relevance_score(signal: Mapping[str, Any], match_terms: Sequence[str]) -> int:
    text = " ".join(
        str(signal.get(key) or "")
        for key in ("title", "url", "quote_or_excerpt", "relevance_reason", "source_type")
    ).casefold()
    score = 0
    for term in match_terms:
        if term.casefold() in text:
            score += 10 if "/" in term or "github.com" in term.casefold() else 5
    if signal.get("source_type") == "story":
        score += 3
    if signal.get("source_type") == "comment":
        score += 2
    engagement = signal.get("engagement")
    if isinstance(engagement, Mapping):
        points = engagement.get("points")
        comments = engagement.get("comments")
        if isinstance(points, int) and points > 0:
            score += min(points, 100) // 20
        if isinstance(comments, int) and comments > 0:
            score += min(comments, 100) // 20
    return score


async def _fetch_hn_hits(
    client: httpx.AsyncClient,
    search_query: HackerNewsSearchQuery,
    *,
    hits_per_query: int,
) -> list[dict[str, Any]]:
    params = {
        "query": search_query.query,
        "tags": search_query.tags,
        "hitsPerPage": hits_per_query,
    }
    response = await client.get(_HN_ALGOLIA_SEARCH_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    hits = payload.get("hits") if isinstance(payload, Mapping) else None
    if not isinstance(hits, list):
        return []
    return [hit for hit in hits if isinstance(hit, dict)]


class HackerNewsCommunitySignalCollector:
    """基于 Algolia HN Search API 的 Hacker News 社区信号采集器。

    该类不依赖登录态，主服务可长期复用实例；也可直接调用下方函数式入口。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        hits_per_query: int = 8,
        max_concurrency: int = 4,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.hits_per_query = hits_per_query
        self.max_concurrency = max_concurrency

    async def collect(
        self,
        project_name: str | None = None,
        repo_url: str | None = None,
        aliases: Sequence[str] | None = None,
        *,
        max_results: int = 12,
    ) -> list[dict[str, Any]]:
        """返回统一 JSON 信号列表。

        字段固定包含：
        platform/url/title/published_at/engagement/stance/quote_or_excerpt/
        source_type/topic_tags/relevance_reason。
        """

        if max_results <= 0:
            return []

        queries = build_hacker_news_search_queries(project_name, repo_url, aliases)
        if not queries:
            return []

        headers = {"User-Agent": _DEFAULT_USER_AGENT}
        timeout = httpx.Timeout(self.timeout_seconds)
        semaphore = asyncio.Semaphore(max(1, self.max_concurrency))
        hits: list[tuple[HackerNewsSearchQuery, dict[str, Any]]] = []

        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            async def fetch_with_limit(search_query: HackerNewsSearchQuery) -> list[tuple[HackerNewsSearchQuery, dict[str, Any]]]:
                async with semaphore:
                    try:
                        query_hits = await _fetch_hn_hits(client, search_query, hits_per_query=self.hits_per_query)
                    except Exception:
                        logger.debug(
                            "hacker news signal search failed: query=%s tags=%s",
                            search_query.query,
                            search_query.tags,
                            exc_info=True,
                        )
                        return []
                    return [(search_query, hit) for hit in query_hits]

            batches = await asyncio.gather(*(fetch_with_limit(query) for query in queries))
            for batch in batches:
                hits.extend(batch)

        signals: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for search_query, hit in hits:
            object_id = str(hit.get("objectID") or hit.get("id") or _hn_item_url(hit))
            if object_id in seen_ids:
                continue
            seen_ids.add(object_id)
            signals.append(
                _hn_hit_to_signal(
                    hit,
                    query=search_query.query,
                    project_name=project_name,
                    repo_url=repo_url,
                    aliases=aliases,
                )
            )

        match_terms = _hn_search_terms(project_name, repo_url, aliases)
        signals.sort(key=lambda signal: _hn_signal_relevance_score(signal, match_terms), reverse=True)
        return signals[:max_results]


async def collect_hacker_news_signals(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    hits_per_query: int = 8,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """函数式入口：根据 GitHub 项目上下文采集 HN story/comment 信号。"""

    collector = HackerNewsCommunitySignalCollector(timeout_seconds=timeout_seconds, hits_per_query=hits_per_query)
    return await collector.collect(project_name=project_name, repo_url=repo_url, aliases=aliases, max_results=max_results)


async def collect_hacker_news_signals_for_repo_context(
    repo_context: Mapping[str, Any],
    *,
    max_results: int = 12,
    hits_per_query: int = 8,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """字典入口：便于主服务直接传 GitHub 项目上下文。"""

    project_name = (
        repo_context.get("project_name")
        or repo_context.get("name")
        or repo_context.get("repo_name")
        or repo_context.get("full_name")
    )
    repo_url = repo_context.get("repo_url") or repo_context.get("html_url") or repo_context.get("url")
    raw_aliases = repo_context.get("aliases") or repo_context.get("alias") or []
    if isinstance(raw_aliases, str):
        aliases = [raw_aliases]
    elif isinstance(raw_aliases, Sequence):
        aliases = [str(alias) for alias in raw_aliases if alias]
    else:
        aliases = []

    return await collect_hacker_news_signals(
        project_name=str(project_name) if project_name else None,
        repo_url=str(repo_url) if repo_url else None,
        aliases=aliases,
        max_results=max_results,
        hits_per_query=hits_per_query,
        timeout_seconds=timeout_seconds,
    )


def collect_hacker_news_signals_sync(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    hits_per_query: int = 8,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """同步入口，供脚本或非 async worker 使用。"""

    return asyncio.run(
        collect_hacker_news_signals(
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
            max_results=max_results,
            hits_per_query=hits_per_query,
            timeout_seconds=timeout_seconds,
        )
    )


def _reddit_user_agent() -> str:
    settings = get_settings()
    return _compact_text(settings.reddit_user_agent) or _DEFAULT_USER_AGENT


def _reddit_oauth_credentials() -> tuple[str, str] | None:
    settings = get_settings()
    client_id = _compact_text(settings.reddit_client_id)
    client_secret = _compact_text(settings.reddit_client_secret)
    if client_id and client_secret:
        return client_id, client_secret
    return None


async def _reddit_get_oauth_access_token(timeout_seconds: float) -> str | None:
    """用 client_credentials 获取 Reddit 官方 OAuth token。

    依据 Reddit Data API 官方要求，优先使用 OAuth。这里做进程内缓存，避免每次搜索都换 token。
    """

    credentials = _reddit_oauth_credentials()
    if credentials is None:
        return None

    now = time.time()
    cached_token = _reddit_oauth_cache.get("access_token")
    expires_at = float(_reddit_oauth_cache.get("expires_at") or 0.0)
    if cached_token and expires_at - now > 30:
        return str(cached_token)

    async with _reddit_oauth_lock:
        now = time.time()
        cached_token = _reddit_oauth_cache.get("access_token")
        expires_at = float(_reddit_oauth_cache.get("expires_at") or 0.0)
        if cached_token and expires_at - now > 30:
            return str(cached_token)

        client_id, client_secret = credentials
        headers = {
            "User-Agent": _reddit_user_agent(),
        }
        data = {
            "grant_type": "client_credentials",
        }
        timeout = httpx.Timeout(timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = await client.post(_REDDIT_OAUTH_TOKEN_URL, data=data, auth=(client_id, client_secret))
            response.raise_for_status()
            payload = response.json()

        access_token = _compact_text(str(payload.get("access_token") or ""))
        expires_in = payload.get("expires_in")
        if not access_token:
            return None
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        _reddit_oauth_cache["access_token"] = access_token
        _reddit_oauth_cache["expires_at"] = time.time() + max(60.0, ttl)
        return access_token


async def _fetch_reddit_oauth_search_signals(
    *,
    token: str,
    query: str,
    limit: int,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": _reddit_user_agent(),
        "Accept": "application/json",
    }
    params = {
        "q": query,
        "sort": "relevance",
        "t": "all",
        "limit": max(1, limit),
        "type": "link",
        "raw_json": 1,
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout_seconds), follow_redirects=True) as client:
        response = await client.get(f"{_REDDIT_OAUTH_BASE_URL}/search", params=params)
        response.raise_for_status()
        payload = response.json()

    children = ((payload.get("data") or {}).get("children") or []) if isinstance(payload, Mapping) else []
    signals: list[dict[str, Any]] = []
    if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            signal = _reddit_json_child_to_signal(
                child,
                query=query,
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
            )
            if signal:
                if isinstance(signal.get("engagement"), Mapping):
                    signal["engagement"] = {
                        **dict(signal["engagement"]),
                        "oauth_api": True,
                    }
                signal["source_type"] = "post_oauth"
                signals.append(signal)
    return signals


def _reddit_search_terms(project_name: str | None, repo_url: str | None, aliases: Sequence[str] | None) -> list[str]:
    """从 GitHub 项目上下文提取 Reddit 搜索关键词。"""

    owner, repo_name = _repo_owner_name(repo_url)
    if owner and repo_name:
        terms = [
            f"{owner}/{repo_name}",
            f"github.com/{owner}/{repo_name}",
            f"https://github.com/{owner}/{repo_name}",
            f"http://github.com/{owner}/{repo_name}",
        ]
        for alias in aliases or []:
            clean_alias = _compact_text(alias)
            if "/" in clean_alias or "github.com/" in clean_alias.casefold() or len(clean_alias.split()) >= 2:
                terms.append(clean_alias)
        terms.extend([repo_name, owner])
    else:
        terms = [*_normalize_aliases(project_name, repo_url, aliases), *_github_repo_url_variants(repo_url)]

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = _compact_text(term)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped[:_MAX_QUERY_TERMS]


def build_reddit_search_queries(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
) -> list[str]:
    """构建 Reddit 公开搜索 query；保持简单可解释，避免过度构造布尔表达式。"""

    return _reddit_search_terms(project_name, repo_url, aliases)


def _reddit_post_url(data: Mapping[str, Any]) -> str:
    permalink = _compact_text(str(data.get("permalink") or ""))
    if permalink:
        if permalink.startswith("http://") or permalink.startswith("https://"):
            return permalink
        return "https://www.reddit.com" + (permalink if permalink.startswith("/") else f"/{permalink}")
    url = _compact_text(str(data.get("url") or ""))
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://www.reddit.com/search/"


def _reddit_published_at(data: Mapping[str, Any]) -> str:
    created_utc = data.get("created_utc")
    if isinstance(created_utc, (int, float)):
        return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    created = _compact_text(str(data.get("created") or ""))
    return _parse_datetime_to_iso(created) or created or "unknown"


def _reddit_excerpt(data: Mapping[str, Any]) -> str:
    for key in ("selftext", "body", "title", "url"):
        value = _html_to_plain_text(str(data.get(key) or ""), max_chars=_REDDIT_EXCERPT_CHARS)
        if value:
            return value
    return "unknown"


def _reddit_engagement(data: Mapping[str, Any]) -> dict[str, Any] | str:
    engagement: dict[str, Any] = {}
    for source_key, target_key in (
        ("score", "score"),
        ("ups", "upvotes"),
        ("num_comments", "comments"),
        ("upvote_ratio", "upvote_ratio"),
        ("subreddit", "subreddit"),
        ("author", "author"),
        ("id", "reddit_id"),
    ):
        value = data.get(source_key)
        if value is not None:
            engagement[target_key] = value
    external_url = data.get("url")
    if external_url:
        engagement["external_url"] = str(external_url)
    return engagement or "unknown"


def _reddit_topic_tags(data: Mapping[str, Any], title: str, excerpt: str, match_terms: Sequence[str]) -> list[str]:
    tags = ["reddit", "post"]
    subreddit = _compact_text(str(data.get("subreddit") or ""))
    if subreddit:
        tags.append(f"r_{subreddit.lower()[:50]}")

    text = f"{title} {excerpt} {data.get('url') or ''}".casefold()
    for keyword, tag in (
        ("github", "github"),
        ("open source", "open_source"),
        ("ai", "ai"),
        ("agent", "agent"),
        ("llm", "llm"),
        ("developer", "developer_tools"),
        ("launch", "launch"),
    ):
        if keyword in text:
            tags.append(tag)

    for term in match_terms[:5]:
        if term.casefold() not in text:
            continue
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", term.strip().lower()).strip("_")
        if safe:
            tags.append(safe[:40])

    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _reddit_relevance_reason(
    *,
    query: str,
    title: str,
    excerpt: str,
    url: str,
    match_terms: Sequence[str],
) -> str:
    haystack = f"{title}\n{excerpt}\n{url}".casefold()
    matched = [term for term in match_terms if term.casefold() in haystack]
    if matched:
        return f"Reddit 搜索命中项目上下文关键词：{', '.join(dict.fromkeys(matched[:5]))}"
    return f"Reddit 公开搜索命中，query={query}；建议人工复核是否同名项目"


def _reddit_json_child_to_signal(
    child: Mapping[str, Any],
    *,
    query: str,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> dict[str, Any] | None:
    data = child.get("data") if isinstance(child.get("data"), Mapping) else child
    if not isinstance(data, Mapping):
        return None

    title = _compact_text(str(data.get("title") or data.get("link_title") or "unknown"))
    excerpt = _reddit_excerpt(data)
    url = _reddit_post_url(data)
    match_terms = _reddit_search_terms(project_name, repo_url, aliases)
    signal = _unknown_signal(url)
    signal.update(
        {
            "platform": "Reddit",
            "url": url,
            "title": title or "unknown",
            "published_at": _reddit_published_at(data),
            "engagement": _reddit_engagement(data),
            "stance": "unknown",
            "quote_or_excerpt": excerpt,
            "source_type": "post",
            "topic_tags": _reddit_topic_tags(data, title, excerpt, match_terms),
            "relevance_reason": _reddit_relevance_reason(
                query=query,
                title=title,
                excerpt=excerpt,
                url=url,
                match_terms=match_terms,
            ),
        }
    )
    return signal


async def _fetch_reddit_json_signals(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    query: str,
    limit: int,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> list[dict[str, Any]]:
    response = await client.get(
        endpoint,
        params={"q": query, "sort": "relevance", "t": "all", "limit": max(1, limit), "raw_json": 1},
    )
    if response.status_code != 200:
        raise httpx.HTTPStatusError("reddit search returned non-200", request=response.request, response=response)
    payload = response.json()
    children = ((payload.get("data") or {}).get("children") or []) if isinstance(payload, Mapping) else []
    signals: list[dict[str, Any]] = []
    if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            signal = _reddit_json_child_to_signal(
                child,
                query=query,
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
            )
            if signal:
                signals.append(signal)
    return signals


async def _fetch_old_reddit_html_signals(
    client: httpx.AsyncClient,
    *,
    query: str,
    limit: int,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> list[dict[str, Any]]:
    response = await client.get(
        _REDDIT_OLD_SEARCH_URL,
        params={"q": query, "sort": "relevance", "t": "all", "restrict_sr": "off"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    match_terms = _reddit_search_terms(project_name, repo_url, aliases)
    signals: list[dict[str, Any]] = []
    for result in soup.select(".search-result, .thing"):
        link = result.select_one("a.search-title, a.title, a[href]")
        if not link:
            continue
        href = link.get("href") or ""
        url = _clean_url(href if href.startswith("http") else f"https://old.reddit.com{href}")
        if not url:
            continue
        title = _compact_text(link.get_text(" ", strip=True)) or "unknown"
        snippet_node = result.select_one(".search-result-body, .search-expando, .md, .usertext-body")
        excerpt = _compact_text(snippet_node.get_text(" ", strip=True) if snippet_node else title, max_chars=_REDDIT_EXCERPT_CHARS)
        subreddit_node = result.select_one(".search-subreddit-link, .subreddit, a.subreddit")
        subreddit = _compact_text(subreddit_node.get_text(" ", strip=True) if subreddit_node else "")
        data = {"subreddit": subreddit.removeprefix("r/"), "url": url}
        signal = _unknown_signal(url)
        signal.update(
            {
                "platform": "Reddit",
                "url": url,
                "title": title,
                "published_at": "unknown",
                "engagement": {"subreddit": subreddit} if subreddit else "unknown",
                "stance": "unknown",
                "quote_or_excerpt": excerpt or "unknown",
                "source_type": "post",
                "topic_tags": _reddit_topic_tags(data, title, excerpt, match_terms),
                "relevance_reason": _reddit_relevance_reason(
                    query=query,
                    title=title,
                    excerpt=excerpt,
                    url=url,
                    match_terms=match_terms,
                ),
            }
        )
        signals.append(signal)
        if len(signals) >= limit:
            break
    return signals


def _reddit_browser_profile_dir() -> Path | None:
    settings = get_settings()
    raw = _compact_text(settings.reddit_browser_profile_dir)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (_PROJECT_ROOT / path).resolve()
    return path


def _reddit_browser_channel() -> str | None:
    settings = get_settings()
    channel = _compact_text(settings.reddit_browser_channel)
    return channel or None


def _reddit_browser_headless() -> bool:
    settings = get_settings()
    return bool(settings.reddit_browser_headless)


def _parse_human_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _compact_text(str(value)).replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    suffix = text[-1].lower()
    if suffix == "k":
        multiplier = 1_000.0
        text = text[:-1]
    elif suffix == "m":
        multiplier = 1_000_000.0
        text = text[:-1]
    elif suffix == "万":
        multiplier = 10_000.0
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def _reddit_browser_requires_login(body_text: str, current_url: str) -> bool:
    lowered = body_text.casefold()
    if "blocked by network security" in lowered:
        return True
    if "/login" in current_url:
        return True
    if "log in" in lowered and "reddit account" in lowered:
        return True
    return False


def _reddit_browser_item_to_signal(
    item: Mapping[str, Any],
    *,
    query: str,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> dict[str, Any] | None:
    raw_url = _compact_text(str(item.get("href") or item.get("url") or ""))
    if not raw_url:
        return None
    if raw_url.startswith("/"):
        raw_url = f"https://www.reddit.com{raw_url}"
    url = _clean_url(raw_url)
    if not url:
        return None

    title = _compact_text(str(item.get("title") or ""))
    excerpt = _compact_text(str(item.get("excerpt") or ""), max_chars=_REDDIT_EXCERPT_CHARS) or title or "unknown"
    subreddit = _compact_text(str(item.get("subreddit") or "")).removeprefix("r/")
    published_at_raw = _compact_text(str(item.get("created") or item.get("published_at") or ""))
    published_at = _parse_datetime_to_iso(published_at_raw) or published_at_raw or "unknown"

    score = _parse_human_count(item.get("score"))
    comments = _parse_human_count(item.get("comments"))
    engagement: dict[str, Any] = {}
    if score is not None:
        engagement["score"] = score
    if comments is not None:
        engagement["comments"] = comments
    if subreddit:
        engagement["subreddit"] = subreddit

    data = {
        "subreddit": subreddit,
        "url": url,
        "score": score,
        "num_comments": comments,
        "created": published_at_raw,
    }
    match_terms = _reddit_search_terms(project_name, repo_url, aliases)
    signal = _unknown_signal(url)
    signal.update(
        {
            "platform": "Reddit",
            "url": url,
            "title": title or "unknown",
            "published_at": published_at,
            "engagement": engagement or "unknown",
            "stance": "unknown",
            "quote_or_excerpt": excerpt,
            "source_type": "post_browser",
            "topic_tags": _reddit_topic_tags(data, title, excerpt, match_terms),
            "relevance_reason": _reddit_relevance_reason(
                query=query,
                title=title,
                excerpt=excerpt,
                url=url,
                match_terms=match_terms,
            ),
        }
    )
    return signal


async def _fetch_reddit_browser_signals(
    *,
    query: str,
    limit: int,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    profile_dir = _reddit_browser_profile_dir()
    if profile_dir is None:
        raise RuntimeError("未配置 REDDIT_BROWSER_PROFILE_DIR")
    if not profile_dir.exists():
        raise FileNotFoundError(f"Reddit 浏览器登录态目录不存在：{profile_dir}")

    from playwright.async_api import async_playwright

    search_url = f"https://www.reddit.com/search/?q={quote_plus(query)}&sort=relevance&t=all"
    browser_timeout_ms = int(min(max(timeout_seconds, 15.0), 25.0) * 1000)
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": _reddit_browser_headless(),
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    channel = _reddit_browser_channel()
    if channel:
        launch_kwargs["channel"] = channel

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            response = None
            try:
                response = await page.goto(search_url, wait_until="commit", timeout=browser_timeout_ms)
            except Exception as exc:
                logger.debug("reddit browser navigation did not finish cleanly: query=%s error=%s", query, exc)
                if not page.url.startswith("https://www.reddit.com/"):
                    await page.goto(search_url, wait_until="commit", timeout=10_000)
            if response is not None and response.status >= 400:
                logger.debug("reddit browser fetch status=%s query=%s", response.status, query)
            await page.wait_for_timeout(min(4_500, max(1_500, int(timeout_seconds * 150))))
            body_text = await page.locator("body").inner_text(timeout=10_000)
            if _reddit_browser_requires_login(body_text, page.url):
                raise RuntimeError("Reddit 浏览器 profile 尚未登录，或登录态已失效")

            raw_items = await page.evaluate(
                """
                (limit) => {
                  const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const items = [];
                  const seen = new Set();
                  const push = (item) => {
                    if (!item) return;
                    let href = item.href || item.url || '';
                    if (!href) return;
                    if (href.startsWith('/')) href = `https://www.reddit.com${href}`;
                    if (!/\\/comments\\//.test(href)) return;
                    if (seen.has(href)) return;
                    seen.add(href);
                    items.push({
                      href,
                      title: norm(item.title),
                      excerpt: norm(item.excerpt),
                      subreddit: norm(item.subreddit),
                      score: norm(item.score),
                      comments: norm(item.comments),
                      created: norm(item.created),
                    });
                  };

                  const posts = [...document.querySelectorAll('shreddit-post')];
                  for (const post of posts) {
                    if (items.length >= limit) break;
                    const anchor = post.querySelector('a[href*="/comments/"]');
                    const excerptNode =
                      post.querySelector('[slot="text-body"]') ||
                      post.querySelector('[data-click-id="text"]') ||
                      post.querySelector('[data-testid="post-container"]');
                    push({
                      href:
                        post.getAttribute('permalink') ||
                        post.getAttribute('content-href') ||
                        post.getAttribute('url') ||
                        anchor?.href ||
                        anchor?.getAttribute('href') ||
                        '',
                      title:
                        post.getAttribute('post-title') ||
                        post.getAttribute('aria-label') ||
                        anchor?.textContent ||
                        post.querySelector('h3')?.textContent ||
                        '',
                      excerpt:
                        excerptNode?.textContent ||
                        post.innerText ||
                        '',
                      subreddit:
                        post.getAttribute('subreddit-prefixed-name') ||
                        post.getAttribute('subreddit-name-prefixed') ||
                        post.querySelector('a[href^="/r/"]')?.textContent ||
                        '',
                      score:
                        post.getAttribute('score') ||
                        post.getAttribute('vote-count') ||
                        '',
                      comments:
                        post.getAttribute('comment-count') ||
                        '',
                      created:
                        post.getAttribute('created-timestamp') ||
                        post.querySelector('time')?.getAttribute('datetime') ||
                        post.querySelector('time')?.textContent ||
                        '',
                    });
                  }

                  const readTrackingContext = (root) => {
                    const trackers = [
                      ...(root?.matches?.('search-telemetry-tracker[data-faceplate-tracking-context]') ? [root] : []),
                      ...[...(root?.querySelectorAll?.('search-telemetry-tracker[data-faceplate-tracking-context]') || [])],
                    ];
                    for (const tracker of trackers) {
                      const raw = tracker.getAttribute('data-faceplate-tracking-context');
                      if (!raw) continue;
                      try {
                        const parsed = JSON.parse(raw);
                        if (parsed?.post || parsed?.subreddit) return parsed;
                      } catch (_) {}
                    }
                    return {};
                  };

                  if (items.length === 0) {
                    const units = [...document.querySelectorAll('[data-testid="search-post-unit"]')];
                    for (const unit of units) {
                      if (items.length >= limit) break;
                      const anchor =
                        unit.querySelector('a[data-testid="post-title-text"][href*="/comments/"]') ||
                        unit.querySelector('a[data-testid="post-title"][href*="/comments/"]') ||
                        unit.querySelector('a[href*="/comments/"]');
                      const ctx = readTrackingContext(unit);
                      const text = norm(unit.innerText || anchor?.textContent || '');
                      const scoreMatch = text.match(/([0-9]+(?:\\.[0-9]+)?[kKmM万]?)\\s*(?:upvotes?|votes?|票)/i);
                      const commentsMatch = text.match(/([0-9]+(?:\\.[0-9]+)?[kKmM万]?)\\s*(?:comments?|条评论|评论)/i);
                      const subreddit =
                        ctx?.subreddit?.name ||
                        unit.querySelector('a[href^="/r/"] .truncate')?.textContent ||
                        unit.querySelector('a[href^="/r/"]')?.textContent ||
                        '';
                      const createdMatch = text.match(/r\\/[^\\s]+\\s*·\\s*([^\\s]+(?:前|ago)?)/i);
                      push({
                        href: anchor?.href || anchor?.getAttribute('href') || '',
                        title:
                          ctx?.post?.title ||
                          anchor?.getAttribute('aria-label') ||
                          anchor?.textContent ||
                          '',
                        excerpt: text,
                        subreddit,
                        score: scoreMatch ? scoreMatch[1] : '',
                        comments: commentsMatch ? commentsMatch[1] : '',
                        created: createdMatch ? createdMatch[1] : '',
                      });
                    }
                  }

                  if (items.length === 0) {
                    const anchors = [...document.querySelectorAll('a[href*="/comments/"]')];
                    for (const anchor of anchors) {
                      if (items.length >= limit) break;
                      const container =
                        anchor.closest('[data-testid="post-container"]') ||
                        anchor.closest('article') ||
                        anchor.closest('shreddit-post') ||
                        anchor.parentElement;
                      const text = norm(container?.innerText || anchor.textContent || '');
                      const scoreMatch = text.match(/([0-9]+(?:\\.[0-9]+)?[kKmM万]?)\\s*(?:upvotes?|votes?|票)/i);
                      const commentsMatch = text.match(/([0-9]+(?:\\.[0-9]+)?[kKmM万]?)\\s*(?:comments?|条评论|评论)/i);
                      push({
                        href: anchor.href || anchor.getAttribute('href') || '',
                        title:
                          anchor.textContent ||
                          container?.querySelector('h3')?.textContent ||
                          '',
                        excerpt: text,
                        subreddit: container?.querySelector('a[href^="/r/"]')?.textContent || '',
                        score: scoreMatch ? scoreMatch[1] : '',
                        comments: commentsMatch ? commentsMatch[1] : '',
                        created:
                          container?.querySelector('time')?.getAttribute('datetime') ||
                          container?.querySelector('time')?.textContent ||
                          '',
                      });
                    }
                  }

                  return items.slice(0, limit);
                }
                """,
                max(1, limit),
            )
        finally:
            await context.close()

    signals: list[dict[str, Any]] = []
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            signal = _reddit_browser_item_to_signal(
                item,
                query=query,
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
            )
            if signal:
                signals.append(signal)
    return signals


def _reddit_signal_relevance_score(signal: Mapping[str, Any], match_terms: Sequence[str]) -> int:
    text = " ".join(
        str(signal.get(key) or "")
        for key in ("title", "url", "quote_or_excerpt", "relevance_reason", "source_type")
    ).casefold()
    score = 0
    for term in match_terms:
        if term.casefold() in text:
            score += 10 if "/" in term or "github.com" in term.casefold() else 5
    engagement = signal.get("engagement")
    if isinstance(engagement, Mapping):
        score += min(int(engagement.get("score") or engagement.get("upvotes") or 0), 500) // 25
        score += min(int(engagement.get("comments") or 0), 200) // 10
    if signal.get("source_type") == "fallback":
        score -= 50
    return score


def _reddit_fallback_signal(query: str, reason: str) -> dict[str, Any]:
    search_url = f"https://www.reddit.com/search/?q={quote_plus(query)}&sort=relevance&t=all"
    signal = _unknown_signal(search_url)
    signal.update(
        {
            "platform": "Reddit",
            "url": search_url,
            "title": f"Reddit 搜索复核入口：{query}",
            "published_at": "unknown",
            "engagement": "unknown",
            "stance": "unknown",
            "quote_or_excerpt": "Reddit 公开接口可能限流、403 或无命中；保留搜索 URL 供人工复核。",
            "source_type": "fallback",
            "topic_tags": ["reddit", "fallback"],
            "relevance_reason": reason,
        }
    )
    return signal


async def collect_reddit_community_signals(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    include_fallback_signal: bool = False,
) -> list[dict[str, Any]]:
    """采集 Reddit 搜索中与 GitHub 项目相关的帖子信号。

    Reddit 对匿名接口限流比较常见，因此实现为：
    1. 优先 Reddit 官方 OAuth Search API；
    2. 再尝试公开 `www.reddit.com/search.json` / `old.reddit.com/search.json`；
    3. JSON 不可用时降级解析 old.reddit.com 搜索页；
    4. 若仍无命中或结果不足，则复用专用浏览器 profile 打开搜索页并从 DOM 抓取帖子。
    """

    if max_results <= 0:
        return []

    queries = build_reddit_search_queries(project_name, repo_url, aliases)
    if not queries:
        return []

    headers = {
        "Accept": "application/json, text/html;q=0.8",
        "User-Agent": _reddit_user_agent(),
    }
    timeout = httpx.Timeout(timeout_seconds)
    signals: list[dict[str, Any]] = []
    failure_notes: list[str] = []
    browser_attempt_errors: list[str] = []

    oauth_token: str | None = None
    try:
        oauth_token = await _reddit_get_oauth_access_token(timeout_seconds)
    except Exception as exc:
        failure_notes.append(f"oauth token error={type(exc).__name__}: {str(exc)[:160]}")
        logger.debug("reddit oauth token fetch failed", exc_info=True)

    browser_profile_dir = _reddit_browser_profile_dir()
    browser_profile_available = bool(browser_profile_dir and browser_profile_dir.exists())
    if browser_profile_available and not oauth_token:
        for query in queries[:1]:
            try:
                signals.extend(
                    await _fetch_reddit_browser_signals(
                        query=query,
                        limit=max_results,
                        project_name=project_name,
                        repo_url=repo_url,
                        aliases=aliases,
                        timeout_seconds=max(timeout_seconds, 20.0),
                    )
                )
            except Exception as exc:
                browser_attempt_errors.append(
                    f"browser profile q={query!r} error={type(exc).__name__}: {str(exc)[:160]}"
                )
                logger.debug("reddit browser search failed: query=%s", query, exc_info=True)
            failure_notes.extend(browser_attempt_errors)

            deduped: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for signal in signals:
                url = str(signal.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                deduped.append(signal)

            if not deduped and include_fallback_signal:
                reason = "Reddit 浏览器登录态搜索无可用命中"
                if failure_notes:
                    reason += "；" + " | ".join(failure_notes[:3])
                deduped.append(_reddit_fallback_signal(query, reason))

            match_terms = _reddit_search_terms(project_name, repo_url, aliases)
            deduped.sort(key=lambda signal: _reddit_signal_relevance_score(signal, match_terms), reverse=True)
            return deduped[:max_results]

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for query in queries[:search_limit]:
            if oauth_token and len(signals) < max_results * 2:
                try:
                    signals.extend(
                        await _fetch_reddit_oauth_search_signals(
                            token=oauth_token,
                            query=query,
                            limit=max_results,
                            project_name=project_name,
                            repo_url=repo_url,
                            aliases=aliases,
                            timeout_seconds=timeout_seconds,
                        )
                    )
                except Exception as exc:
                    failure_notes.append(f"oauth search q={query!r} error={type(exc).__name__}: {str(exc)[:160]}")
                    logger.debug("reddit oauth search failed: query=%s", query, exc_info=True)

            if len(signals) >= max_results:
                break

            for endpoint in _REDDIT_SEARCH_JSON_URLS:
                try:
                    signals.extend(
                        await _fetch_reddit_json_signals(
                            client,
                            endpoint=endpoint,
                            query=query,
                            limit=max_results,
                            project_name=project_name,
                            repo_url=repo_url,
                            aliases=aliases,
                        )
                    )
                except Exception as exc:
                    failure_notes.append(f"{endpoint} q={query!r} error={type(exc).__name__}: {str(exc)[:160]}")
                    logger.debug("reddit json search failed: endpoint=%s query=%s", endpoint, query, exc_info=True)
                if len(signals) >= max_results * 2:
                    break

            if len(signals) < max_results:
                try:
                    signals.extend(
                        await _fetch_old_reddit_html_signals(
                            client,
                            query=query,
                            limit=max_results,
                            project_name=project_name,
                            repo_url=repo_url,
                            aliases=aliases,
                        )
                    )
                except Exception as exc:
                    failure_notes.append(f"old.reddit.com/search q={query!r} error={type(exc).__name__}: {str(exc)[:160]}")
                    logger.debug("old reddit html search failed: query=%s", query, exc_info=True)

            if len(signals) < max_results:
                try:
                    signals.extend(
                        await _fetch_reddit_browser_signals(
                            query=query,
                            limit=max_results,
                            project_name=project_name,
                            repo_url=repo_url,
                            aliases=aliases,
                            timeout_seconds=max(timeout_seconds, 20.0),
                        )
                    )
                except Exception as exc:
                    message = f"browser profile q={query!r} error={type(exc).__name__}: {str(exc)[:160]}"
                    browser_attempt_errors.append(message)
                    logger.debug("reddit browser search failed: query=%s", query, exc_info=True)
                    if isinstance(exc, FileNotFoundError):
                        break
                    if "尚未登录" in str(exc) or "未配置 REDDIT_BROWSER_PROFILE_DIR" in str(exc):
                        break

            if len(signals) >= max_results:
                break

    failure_notes.extend(browser_attempt_errors)

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for signal in signals:
        url = str(signal.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(signal)

    if not deduped and include_fallback_signal:
        reason = "Reddit 官方 OAuth API、公开搜索接口与浏览器登录态搜索均无可用命中"
        if _reddit_oauth_credentials() is None:
            reason = "未配置 Reddit OAuth 凭据，已退回公开搜索与浏览器登录态；公开接口不可用、浏览器未登录或无命中"
        if failure_notes:
            reason += "；" + " | ".join(failure_notes[:3])
        deduped.append(_reddit_fallback_signal(queries[0], reason))

    match_terms = _reddit_search_terms(project_name, repo_url, aliases)
    deduped.sort(key=lambda signal: _reddit_signal_relevance_score(signal, match_terms), reverse=True)
    return deduped[:max_results]


async def collect_reddit_community_signals_for_repo_context(
    repo_context: Mapping[str, Any],
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    include_fallback_signal: bool = False,
) -> list[dict[str, Any]]:
    """接受字典形式 GitHub 项目上下文，便于 worker 直接接入 Reddit 信源。"""

    project_name = (
        repo_context.get("project_name")
        or repo_context.get("name")
        or repo_context.get("repo_name")
        or repo_context.get("full_name")
    )
    repo_url = repo_context.get("repo_url") or repo_context.get("html_url") or repo_context.get("url")
    raw_aliases = repo_context.get("aliases") or repo_context.get("alias") or []
    if isinstance(raw_aliases, str):
        aliases = [raw_aliases]
    elif isinstance(raw_aliases, Sequence):
        aliases = [str(alias) for alias in raw_aliases if alias]
    else:
        aliases = []

    return await collect_reddit_community_signals(
        project_name=str(project_name) if project_name else None,
        repo_url=str(repo_url) if repo_url else None,
        aliases=aliases,
        max_results=max_results,
        search_limit=search_limit,
        timeout_seconds=timeout_seconds,
        include_fallback_signal=include_fallback_signal,
    )


def collect_reddit_community_signals_sync(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    include_fallback_signal: bool = False,
) -> list[dict[str, Any]]:
    """同步入口，供脚本或非 async worker 使用。"""

    return asyncio.run(
        collect_reddit_community_signals(
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
            max_results=max_results,
            search_limit=search_limit,
            timeout_seconds=timeout_seconds,
            include_fallback_signal=include_fallback_signal,
        )
    )


def _platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host == "producthunt.com":
        return "Product Hunt"
    if host.endswith("substack.com"):
        return "Substack"
    if host == "medium.com" or host.endswith(".medium.com"):
        return "Medium"
    if host == "dev.to":
        return "DEV Community"
    if host.endswith("hashnode.dev"):
        return "Hashnode"
    return host or "unknown"


def _source_type_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    compact_host = host.removeprefix("www.")
    if host in _DEFAULT_SOURCE_TYPES_BY_HOST:
        return _DEFAULT_SOURCE_TYPES_BY_HOST[host]
    if compact_host in _DEFAULT_SOURCE_TYPES_BY_HOST:
        return _DEFAULT_SOURCE_TYPES_BY_HOST[compact_host]
    path = parsed.path.lower()
    if "newsletter" in path or "substack" in compact_host:
        return "newsletter"
    if any(part in path for part in ("/blog", "/posts", "/articles", "/p/")):
        return "technical_blog"
    return "web_article"


def _build_search_queries(project_name: str | None, repo_url: str | None, aliases: Sequence[str] | None) -> list[str]:
    terms = _normalize_aliases(project_name, repo_url, aliases)
    owner, repo_name = _repo_owner_name(repo_url)
    repo_full_name = f"{owner}/{repo_name}" if owner and repo_name else None

    queries: list[str] = []
    for term in terms[:_MAX_QUERY_TERMS]:
        quoted = f'"{term}"'
        queries.extend(
            [
                f"site:producthunt.com/posts {quoted}",
                f'{quoted} "Product Hunt"',
                f'{quoted} "GitHub" "blog"',
                f'{quoted} "newsletter"',
                f'{quoted} "launch" "technical blog"',
            ]
        )
    if repo_url:
        queries.extend([f'"{repo_url}"', f'"{repo_url}" "Product Hunt"'])
    if repo_full_name:
        queries.extend([f'"{repo_full_name}" "newsletter"', f'"{repo_full_name}" "blog"'])

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


async def _duckduckgo_candidates(
    client: httpx.AsyncClient,
    query: str,
    *,
    max_results: int,
) -> list[CommunitySignalCandidate]:
    response = await client.get(_DDG_HTML_URL, params={"q": query})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    candidates: list[CommunitySignalCandidate] = []
    for result in soup.select(".result"):
        link = result.select_one("a.result__a") or result.select_one("a[href]")
        url = _decode_duckduckgo_href(link.get("href") if link else None)
        if not url:
            continue
        title = _compact_text(link.get_text(" ", strip=True) if link else None)
        snippet_node = result.select_one(".result__snippet")
        snippet = _compact_text(snippet_node.get_text(" ", strip=True) if snippet_node else None)
        candidates.append(CommunitySignalCandidate(url=url, title=title or None, snippet=snippet or None, source_query=query))
        if len(candidates) >= max_results:
            break
    return candidates


async def discover_community_signal_candidates(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    search_limit: int = 24,
    timeout_seconds: float = 12.0,
) -> list[CommunitySignalCandidate]:
    """用搜索引擎发现 Product Hunt、独立博客、Newsletter/技术文章候选 URL。"""

    queries = _build_search_queries(project_name, repo_url, aliases)
    if not queries:
        return []

    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    candidates: list[CommunitySignalCandidate] = []
    seen_urls: set[str] = set()
    per_query_limit = 5
    async with httpx.AsyncClient(headers=headers, timeout=timeout_seconds, follow_redirects=True) as client:
        for query in queries[: max(1, search_limit)]:
            if len(candidates) >= search_limit:
                break
            try:
                found = await _duckduckgo_candidates(client, query, max_results=per_query_limit)
            except Exception:
                logger.debug("community signal search failed: %s", query, exc_info=True)
                continue
            for candidate in found:
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                candidates.append(candidate)
                if len(candidates) >= search_limit:
                    break
    return candidates


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if node and node.get("content"):
            return _compact_text(str(node["content"]))
    return ""


def _extract_title(soup: BeautifulSoup, fallback: str | None) -> str:
    meta_title = _meta_content(soup, "og:title", "twitter:title")
    if meta_title:
        return meta_title
    if soup.title:
        title = _compact_text(soup.title.get_text(" ", strip=True))
        if title:
            return title
    h1 = soup.find("h1")
    if h1:
        title = _compact_text(h1.get_text(" ", strip=True))
        if title:
            return title
    return _compact_text(fallback) or "unknown"


def _extract_published_at(soup: BeautifulSoup) -> str:
    candidates = [
        _meta_content(soup, "article:published_time", "datePublished", "pubdate", "publish_date"),
    ]
    time_node = soup.find("time")
    if time_node:
        candidates.extend([str(time_node.get("datetime") or ""), time_node.get_text(" ", strip=True)])

    for candidate in candidates:
        clean = _compact_text(candidate)
        if not clean:
            continue
        parsed = _parse_datetime_to_iso(clean)
        return parsed or clean
    return "unknown"


def _parse_datetime_to_iso(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.removesuffix("Z") + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.isoformat()
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _visible_text(soup: BeautifulSoup) -> str:
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    return _compact_text(soup.get_text(" ", strip=True))


def _extract_excerpt(
    soup: BeautifulSoup,
    page_text: str,
    *,
    fallback_snippet: str | None,
    match_terms: Sequence[str],
) -> str:
    description = _meta_content(soup, "og:description", "twitter:description", "description")
    if description:
        return _compact_text(description, max_chars=_TEXT_SNIPPET_CHARS)

    lower_text = page_text.casefold()
    for term in match_terms:
        idx = lower_text.find(term.casefold())
        if idx < 0:
            continue
        start = max(0, idx - 160)
        end = min(len(page_text), idx + len(term) + 260)
        return _compact_text(page_text[start:end], max_chars=_TEXT_SNIPPET_CHARS)

    snippet = _compact_text(fallback_snippet, max_chars=_TEXT_SNIPPET_CHARS)
    if snippet:
        return snippet
    return _compact_text(page_text, max_chars=_TEXT_SNIPPET_CHARS) or "unknown"


def _extract_engagement(text: str, url: str) -> dict[str, str] | str:
    engagement: dict[str, str] = {}
    for key, pattern in _ENGAGEMENT_PATTERNS.items():
        match = pattern.search(text)
        if match:
            engagement[key] = match.group(1)

    if _platform_from_url(url) == "Product Hunt" and "upvotes" not in engagement:
        product_hunt_vote = re.search(r"\b([0-9][0-9,\.]*[kKmM]?)\s*(?:▲|upvoted)", text, flags=re.IGNORECASE)
        if product_hunt_vote:
            engagement["upvotes"] = product_hunt_vote.group(1)

    return engagement or "unknown"


def _topic_tags(url: str, title: str, match_terms: Sequence[str]) -> list[str]:
    tags: list[str] = []
    source_type = _source_type_from_url(url)
    if source_type != "unknown":
        tags.append(source_type)
    platform = _platform_from_url(url)
    if platform != "unknown":
        tags.append(platform.lower().replace(" ", "_"))

    text = f"{title} {url}".casefold()
    for keyword, tag in (
        ("ai", "ai"),
        ("agent", "agent"),
        ("llm", "llm"),
        ("open source", "open_source"),
        ("developer", "developer_tools"),
        ("github", "github"),
    ):
        if keyword in text:
            tags.append(tag)
    for term in match_terms[:3]:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", term.strip().lower()).strip("_")
        if safe:
            tags.append(safe[:40])

    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _relevance_reason(
    *,
    title: str,
    page_text: str,
    candidate: CommunitySignalCandidate,
    match_terms: Sequence[str],
    repo_url: str | None,
) -> str:
    haystack = f"{title}\n{candidate.snippet or ''}\n{page_text[:5000]}".casefold()
    matched = [term for term in match_terms if term.casefold() in haystack]
    if repo_url and repo_url.casefold() in haystack:
        matched.append(repo_url)
    if matched:
        return f"页面标题、摘要或正文命中项目上下文关键词：{', '.join(dict.fromkeys(matched[:5]))}"
    if candidate.source_query:
        return f"由搜索查询发现，查询为：{candidate.source_query}"
    return "候选 URL 来自外部搜索或调用方传入，需人工复核相关性"


def _candidate_from_url(url: str) -> CommunitySignalCandidate | None:
    clean = _clean_url(url)
    if not clean:
        return None
    return CommunitySignalCandidate(url=clean)


async def _fetch_signal_page(
    client: httpx.AsyncClient,
    candidate: CommunitySignalCandidate,
    *,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> dict[str, Any] | None:
    try:
        response = await client.get(candidate.url)
        response.raise_for_status()
    except Exception:
        logger.debug("community signal page fetch failed: %s", candidate.url, exc_info=True)
        signal = _unknown_signal(candidate.url)
        signal.update(
            {
                "platform": _platform_from_url(candidate.url),
                "title": candidate.title or "unknown",
                "quote_or_excerpt": candidate.snippet or "unknown",
                "source_type": _source_type_from_url(candidate.url),
                "topic_tags": _topic_tags(candidate.url, candidate.title or "", _normalize_aliases(project_name, repo_url, aliases)),
                "relevance_reason": (
                    f"抓取页面失败，保留搜索结果候选；来源查询：{candidate.source_query}"
                    if candidate.source_query
                    else "抓取页面失败，保留调用方传入 URL 候选"
                ),
            }
        )
        return signal

    html = response.text[:_MAX_FETCH_CHARS]
    soup = BeautifulSoup(html, "html.parser")
    match_terms = _normalize_aliases(project_name, repo_url, aliases)
    page_text = _visible_text(soup)
    title = _extract_title(soup, candidate.title)

    signal = _unknown_signal(str(response.url))
    signal.update(
        {
            "platform": _platform_from_url(str(response.url)),
            "url": _clean_url(str(response.url)) or str(response.url),
            "title": title,
            "published_at": _extract_published_at(soup),
            "engagement": _extract_engagement(page_text, str(response.url)),
            "stance": "unknown",
            "quote_or_excerpt": _extract_excerpt(
                soup,
                page_text,
                fallback_snippet=candidate.snippet,
                match_terms=match_terms,
            ),
            "source_type": _source_type_from_url(str(response.url)),
            "topic_tags": _topic_tags(str(response.url), title, match_terms),
            "relevance_reason": _relevance_reason(
                title=title,
                page_text=page_text,
                candidate=candidate,
                match_terms=match_terms,
                repo_url=repo_url,
            ),
        }
    )
    return signal


def _signal_relevance_score(signal: Mapping[str, Any], match_terms: Sequence[str]) -> int:
    text = " ".join(
        str(signal.get(key) or "")
        for key in ("title", "url", "quote_or_excerpt", "relevance_reason", "platform", "source_type")
    ).casefold()
    score = 0
    for term in match_terms:
        if term.casefold() in text:
            score += 5
    if signal.get("source_type") == "product_hunt":
        score += 4
    if signal.get("source_type") in {"technical_blog", "newsletter", "technical_article"}:
        score += 2
    if signal.get("quote_or_excerpt") != "unknown":
        score += 1
    return score


async def collect_community_signals(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    candidate_urls: Sequence[str] | None = None,
    max_results: int = 12,
    search_limit: int = 24,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """采集 Product Hunt / 通用技术博客 / Newsletter 的社区传播信号。

    输入只依赖 GitHub 项目上下文：项目名、repo URL 和别名。函数先通过 DuckDuckGo
    HTML 搜索发现候选 URL，再对候选页面做轻量定向抓取，最终输出字段稳定的 JSON
    列表。调用方也可以传入 `candidate_urls`，用于绕过搜索或追加人工候选。
    """

    if max_results <= 0:
        return []

    candidates: list[CommunitySignalCandidate] = []
    seen_urls: set[str] = set()
    for url in candidate_urls or []:
        candidate = _candidate_from_url(url)
        if candidate and candidate.url not in seen_urls:
            seen_urls.add(candidate.url)
            candidates.append(candidate)

    discovered = await discover_community_signal_candidates(
        project_name=project_name,
        repo_url=repo_url,
        aliases=aliases,
        search_limit=search_limit,
        timeout_seconds=timeout_seconds,
    )
    for candidate in discovered:
        if candidate.url not in seen_urls:
            seen_urls.add(candidate.url)
            candidates.append(candidate)

    if not candidates:
        return []

    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    timeout = httpx.Timeout(timeout_seconds)
    semaphore = asyncio.Semaphore(5)

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        async def fetch_with_limit(candidate: CommunitySignalCandidate) -> dict[str, Any] | None:
            async with semaphore:
                return await _fetch_signal_page(
                    client,
                    candidate,
                    project_name=project_name,
                    repo_url=repo_url,
                    aliases=aliases,
                )

        fetched = await asyncio.gather(*(fetch_with_limit(candidate) for candidate in candidates), return_exceptions=True)

    signals: list[dict[str, Any]] = []
    for item in fetched:
        if isinstance(item, Exception):
            logger.debug("community signal candidate failed", exc_info=item)
            continue
        if item:
            signals.append(item)

    match_terms = _normalize_aliases(project_name, repo_url, aliases)
    signals.sort(key=lambda signal: _signal_relevance_score(signal, match_terms), reverse=True)
    return signals[:max_results]


async def collect_community_signals_for_repo_context(
    repo_context: Mapping[str, Any],
    *,
    max_results: int = 12,
    search_limit: int = 24,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """接受字典形式的 GitHub 项目上下文，便于 worker 直接接入。

    支持常见键名：
    - project_name/name/repo_name/full_name
    - repo_url/html_url/url
    - aliases/alias
    """

    project_name = (
        repo_context.get("project_name")
        or repo_context.get("name")
        or repo_context.get("repo_name")
        or repo_context.get("full_name")
    )
    repo_url = repo_context.get("repo_url") or repo_context.get("html_url") or repo_context.get("url")
    raw_aliases = repo_context.get("aliases") or repo_context.get("alias") or []
    if isinstance(raw_aliases, str):
        aliases = [raw_aliases]
    elif isinstance(raw_aliases, Sequence):
        aliases = [str(alias) for alias in raw_aliases if alias]
    else:
        aliases = []

    return await collect_community_signals(
        project_name=str(project_name) if project_name else None,
        repo_url=str(repo_url) if repo_url else None,
        aliases=aliases,
        max_results=max_results,
        search_limit=search_limit,
        timeout_seconds=timeout_seconds,
    )


def collect_community_signals_sync(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    candidate_urls: Sequence[str] | None = None,
    max_results: int = 12,
    search_limit: int = 24,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """同步入口，供脚本或非 async worker 使用。"""

    return asyncio.run(
        collect_community_signals(
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
            candidate_urls=candidate_urls,
            max_results=max_results,
            search_limit=search_limit,
            timeout_seconds=timeout_seconds,
        )
    )


def _linux_do_search_terms(project_name: str | None, repo_url: str | None, aliases: Sequence[str] | None) -> list[str]:
    """从 GitHub 项目上下文生成 Linux.do/Discourse 搜索词。"""

    owner, repo_name = _repo_owner_name(repo_url)
    if owner and repo_name:
        terms = [
            f"{owner}/{repo_name}",
            f"github.com/{owner}/{repo_name}",
            f"https://github.com/{owner}/{repo_name}",
            f"http://github.com/{owner}/{repo_name}",
        ]
        for alias in aliases or []:
            clean_alias = _compact_text(alias)
            if "/" in clean_alias or "github.com/" in clean_alias.casefold() or len(clean_alias.split()) >= 2:
                terms.append(clean_alias)
        terms.append(repo_name)
    else:
        terms = [*_normalize_aliases(project_name, repo_url, aliases), *_github_repo_url_variants(repo_url)]

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = _compact_text(term)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped[:_MAX_QUERY_TERMS]


def _linux_do_headers(cookie_header: str | None = None) -> dict[str, str]:
    """构造 Linux.do 请求头；cookie_header 可接入浏览器导出的登录态。"""

    headers = {
        "Accept": "application/json, application/rss+xml;q=0.9, text/html;q=0.8",
        "User-Agent": _DEFAULT_USER_AGENT,
        "Referer": _LINUX_DO_BASE_URL + "/",
    }
    if cookie_header:
        # 只接受调用方显式传入的 Cookie 字符串，不在本机主动读取浏览器配置或凭据。
        headers["Cookie"] = cookie_header
    return headers


def _linux_do_topic_url(topic_id: Any, slug: Any = None, post_number: Any = None) -> str:
    safe_slug = str(slug or "topic").strip("/") or "topic"
    url = f"{_LINUX_DO_BASE_URL}/t/{safe_slug}/{topic_id}"
    if post_number and str(post_number) not in {"0", "1"}:
        url = f"{url}/{post_number}"
    return url


def _linux_do_topic_tags(raw_tags: Any, title: str, excerpt: str, match_terms: Sequence[str]) -> list[str]:
    tags = ["linux_do", "discourse"]
    if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes)):
        for tag in raw_tags:
            clean = re.sub(r"[^a-zA-Z0-9_.\-\u4e00-\u9fff]+", "_", str(tag).strip().lower()).strip("_")
            if clean:
                tags.append(clean[:50])

    text = f"{title} {excerpt}".casefold()
    for keyword, tag in (
        ("github", "github"),
        ("开源", "open_source"),
        ("open source", "open_source"),
        ("ai", "ai"),
        ("agent", "agent"),
        ("llm", "llm"),
        ("开发", "developer_tools"),
    ):
        if keyword in text:
            tags.append(tag)

    for term in match_terms[:5]:
        if term.casefold() not in text:
            continue
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", term.strip().lower()).strip("_")
        if safe:
            tags.append(safe[:40])

    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _linux_do_engagement(topic: Mapping[str, Any] | None, post: Mapping[str, Any] | None = None) -> dict[str, Any] | str:
    topic = topic or {}
    post = post or {}
    engagement: dict[str, Any] = {}
    for source, target in (
        (topic.get("views"), "views"),
        (topic.get("posts_count"), "posts"),
        (topic.get("reply_count"), "replies"),
        (topic.get("like_count"), "topic_likes"),
        (post.get("like_count"), "post_likes"),
        (post.get("post_number"), "post_number"),
        (post.get("username"), "author"),
    ):
        if source is not None:
            engagement[target] = source
    return engagement or "unknown"


def _linux_do_relevance_reason(
    *,
    query: str | None,
    title: str,
    excerpt: str,
    url: str,
    match_terms: Sequence[str],
    fallback_note: str | None = None,
) -> str:
    haystack = f"{title}\n{excerpt}\n{url}".casefold()
    matched = [term for term in match_terms if term.casefold() in haystack]
    prefix = f"{fallback_note}；" if fallback_note else ""
    if matched:
        return f"{prefix}Linux.do 内容命中项目上下文关键词：{', '.join(dict.fromkeys(matched[:5]))}"
    if query:
        return f"{prefix}Linux.do Discourse 搜索命中，query={query}；建议人工复核是否同名项目"
    return f"{prefix}来自 Linux.do 公开订阅过滤结果；建议人工复核相关性"


def _linux_do_search_json_to_signals(
    payload: Mapping[str, Any],
    *,
    query: str,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """把 Discourse `/search.json` 响应转为统一社区信号。"""

    match_terms = _linux_do_search_terms(project_name, repo_url, aliases)
    topics = payload.get("topics")
    posts = payload.get("posts")
    topic_by_id: dict[Any, Mapping[str, Any]] = {}
    if isinstance(topics, Sequence) and not isinstance(topics, (str, bytes)):
        for topic in topics:
            if isinstance(topic, Mapping) and topic.get("id") is not None:
                topic_by_id[topic.get("id")] = topic

    signals: list[dict[str, Any]] = []
    if isinstance(posts, Sequence) and not isinstance(posts, (str, bytes)):
        for post in posts:
            if not isinstance(post, Mapping):
                continue
            topic_id = post.get("topic_id")
            topic = topic_by_id.get(topic_id, {})
            title = _compact_text(str(topic.get("title") or post.get("topic_title") or "unknown"))
            excerpt = _html_to_plain_text(str(post.get("blurb") or post.get("raw") or ""), max_chars=_LINUX_DO_EXCERPT_CHARS)
            slug = topic.get("slug") or post.get("topic_slug")
            post_number = post.get("post_number")
            url = _linux_do_topic_url(topic_id, slug, post_number) if topic_id else _LINUX_DO_BASE_URL
            signal = _unknown_signal(url)
            signal.update(
                {
                    "platform": "Linux.do",
                    "url": url,
                    "title": title or "unknown",
                    "published_at": _parse_datetime_to_iso(str(post.get("created_at") or "")) or str(post.get("created_at") or "unknown"),
                    "engagement": _linux_do_engagement(topic, post),
                    "stance": "unknown",
                    "quote_or_excerpt": excerpt or "unknown",
                    "source_type": "comment" if post_number and str(post_number) != "1" else "topic",
                    "topic_tags": _linux_do_topic_tags(topic.get("tags"), title, excerpt, match_terms),
                    "relevance_reason": _linux_do_relevance_reason(
                        query=query,
                        title=title,
                        excerpt=excerpt,
                        url=url,
                        match_terms=match_terms,
                    ),
                }
            )
            signals.append(signal)

    # 有些 Discourse 搜索只返回 topics 或帖子摘要较少；补充 topic 级信号，避免丢帖子。
    if isinstance(topics, Sequence) and not isinstance(topics, (str, bytes)):
        for topic in topics:
            if not isinstance(topic, Mapping) or topic.get("id") is None:
                continue
            title = _compact_text(str(topic.get("title") or "unknown"))
            excerpt = _compact_text(str(topic.get("fancy_title") or title), max_chars=_LINUX_DO_EXCERPT_CHARS)
            url = _linux_do_topic_url(topic.get("id"), topic.get("slug"))
            signal = _unknown_signal(url)
            signal.update(
                {
                    "platform": "Linux.do",
                    "url": url,
                    "title": title or "unknown",
                    "published_at": _parse_datetime_to_iso(str(topic.get("created_at") or "")) or str(topic.get("created_at") or "unknown"),
                    "engagement": _linux_do_engagement(topic),
                    "stance": "unknown",
                    "quote_or_excerpt": excerpt or "unknown",
                    "source_type": "topic",
                    "topic_tags": _linux_do_topic_tags(topic.get("tags"), title, excerpt, match_terms),
                    "relevance_reason": _linux_do_relevance_reason(
                        query=query,
                        title=title,
                        excerpt=excerpt,
                        url=url,
                        match_terms=match_terms,
                    ),
                }
            )
            signals.append(signal)
    return signals


def _linux_do_parse_pub_date(value: str | None) -> str:
    raw = _compact_text(value)
    if not raw:
        return "unknown"
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return _parse_datetime_to_iso(raw) or raw
    if parsed.tzinfo is None:
        return parsed.isoformat()
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _linux_do_rss_to_signals(
    rss_text: str,
    *,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
    fallback_note: str | None = None,
) -> list[dict[str, Any]]:
    """解析 Linux.do latest.rss，并按项目关键词做本地过滤。"""

    match_terms = _linux_do_search_terms(project_name, repo_url, aliases)
    if not match_terms:
        return []

    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError:
        logger.debug("linux.do rss parse failed", exc_info=True)
        return []

    signals: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _compact_text(item.findtext("title"))
        link = _compact_text(item.findtext("link"))
        description = _html_to_plain_text(item.findtext("description") or "", max_chars=_LINUX_DO_EXCERPT_CHARS)
        haystack = f"{title}\n{description}\n{link}".casefold()
        if not any(term.casefold() in haystack for term in match_terms):
            continue

        tags = [node.text for node in item.findall("category") if node.text]
        signal = _unknown_signal(link or _LINUX_DO_BASE_URL)
        signal.update(
            {
                "platform": "Linux.do",
                "url": link or _LINUX_DO_BASE_URL,
                "title": title or "unknown",
                "published_at": _linux_do_parse_pub_date(item.findtext("pubDate")),
                "engagement": "unknown",
                "stance": "unknown",
                "quote_or_excerpt": description or "unknown",
                "source_type": "topic",
                "topic_tags": _linux_do_topic_tags(tags, title, description, match_terms),
                "relevance_reason": _linux_do_relevance_reason(
                    query=None,
                    title=title,
                    excerpt=description,
                    url=link,
                    match_terms=match_terms,
                    fallback_note=fallback_note,
                ),
            }
        )
        signals.append(signal)
    return signals


def _linux_do_signal_relevance_score(signal: Mapping[str, Any], match_terms: Sequence[str]) -> int:
    text = " ".join(
        str(signal.get(key) or "")
        for key in ("title", "url", "quote_or_excerpt", "relevance_reason", "source_type")
    ).casefold()
    score = 0
    for term in match_terms:
        if term.casefold() in text:
            score += 10 if "/" in term or "github.com" in term.casefold() else 5
    if signal.get("source_type") == "topic":
        score += 2
    if signal.get("source_type") == "comment":
        score += 3
    engagement = signal.get("engagement")
    if isinstance(engagement, Mapping):
        score += min(int(engagement.get("views") or 0), 1000) // 100
        score += min(int(engagement.get("replies") or 0), 100)
    return score


async def collect_linux_do_signals(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    cookie_header: str | None = None,
) -> list[dict[str, Any]]:
    """采集 Linux.do 中与 GitHub 项目相关的帖子/评论信号。

    实现策略：
    1. 优先调用 Linux.do 的 Discourse 公开接口 `/search.json?q=...`；
    2. 如果 `/search.json` 被 Cloudflare/权限/限流拦截，则降级拉取公开
       `latest.rss`，在本地按项目名、repo URL、别名过滤；
    3. 如需要登录态，调用方可从 Chrome/浏览器 DevTools 复制 linux.do 的
       Cookie 请求头后传入 `cookie_header`。本函数不会主动读取本机浏览器
       Cookie 或系统凭据。
    """

    if max_results <= 0:
        return []

    queries = _linux_do_search_terms(project_name, repo_url, aliases)
    if not queries:
        return []

    headers = _linux_do_headers(cookie_header)
    timeout = httpx.Timeout(timeout_seconds)
    signals: list[dict[str, Any]] = []
    search_failure_notes: list[str] = []

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for query in queries[:search_limit]:
            try:
                response = await client.get(_LINUX_DO_SEARCH_JSON_URL, params={"q": query})
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200 or "json" not in content_type.lower():
                    text = response.text[:200].replace("\n", " ")
                    search_failure_notes.append(f"/search.json q={query!r} status={response.status_code} content_type={content_type} body={text!r}")
                    continue
                payload = response.json()
            except Exception as exc:
                search_failure_notes.append(f"/search.json q={query!r} error={type(exc).__name__}: {exc}")
                logger.debug("linux.do search.json failed: %s", query, exc_info=True)
                continue

            signals.extend(
                _linux_do_search_json_to_signals(
                    payload,
                    query=query,
                    project_name=project_name,
                    repo_url=repo_url,
                    aliases=aliases,
                )
            )
            if len(signals) >= max_results * 2:
                break

        if not signals:
            fallback_note = "Linux.do /search.json 当前不可用或无命中，已降级到 latest.rss 公开订阅过滤"
            if search_failure_notes:
                logger.info("linux.do search fallback to RSS: %s", " | ".join(search_failure_notes[:3]))
            try:
                rss_response = await client.get(_LINUX_DO_LATEST_RSS_URL, headers={**headers, "Accept": "application/rss+xml"})
                rss_response.raise_for_status()
                signals.extend(
                    _linux_do_rss_to_signals(
                        rss_response.text,
                        project_name=project_name,
                        repo_url=repo_url,
                        aliases=aliases,
                        fallback_note=fallback_note,
                    )
                )
            except Exception:
                logger.debug("linux.do latest.rss fallback failed", exc_info=True)

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for signal in signals:
        url = str(signal.get("url") or "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(signal)

    match_terms = _linux_do_search_terms(project_name, repo_url, aliases)
    deduped.sort(key=lambda signal: _linux_do_signal_relevance_score(signal, match_terms), reverse=True)
    return deduped[:max_results]


async def collect_linux_do_signals_for_repo_context(
    repo_context: Mapping[str, Any],
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    cookie_header: str | None = None,
) -> list[dict[str, Any]]:
    """接受字典形式 GitHub 项目上下文，便于 worker 直接接入 Linux.do 信源。"""

    project_name = (
        repo_context.get("project_name")
        or repo_context.get("name")
        or repo_context.get("repo_name")
        or repo_context.get("full_name")
    )
    repo_url = repo_context.get("repo_url") or repo_context.get("html_url") or repo_context.get("url")
    raw_aliases = repo_context.get("aliases") or repo_context.get("alias") or []
    if isinstance(raw_aliases, str):
        aliases = [raw_aliases]
    elif isinstance(raw_aliases, Sequence):
        aliases = [str(alias) for alias in raw_aliases if alias]
    else:
        aliases = []

    return await collect_linux_do_signals(
        project_name=str(project_name) if project_name else None,
        repo_url=str(repo_url) if repo_url else None,
        aliases=aliases,
        max_results=max_results,
        search_limit=search_limit,
        timeout_seconds=timeout_seconds,
        cookie_header=cookie_header,
    )


def collect_linux_do_signals_sync(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    max_results: int = 12,
    search_limit: int = 8,
    timeout_seconds: float = 12.0,
    cookie_header: str | None = None,
) -> list[dict[str, Any]]:
    """同步入口，供脚本或非 async worker 使用。"""

    return asyncio.run(
        collect_linux_do_signals(
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
            max_results=max_results,
            search_limit=search_limit,
            timeout_seconds=timeout_seconds,
            cookie_header=cookie_header,
        )
    )


def _extract_repo_context(repo_context: Mapping[str, Any]) -> tuple[str | None, str | None, list[str], list[str]]:
    """从主服务项目上下文中提取社区采集需要的字段。"""

    project_name = (
        repo_context.get("project_name")
        or repo_context.get("name")
        or repo_context.get("repo_name")
        or repo_context.get("full_name")
        or repo_context.get("title")
    )
    repo_url = repo_context.get("repo_url") or repo_context.get("html_url") or repo_context.get("url")
    raw_aliases = repo_context.get("aliases") or repo_context.get("alias") or []
    aliases: list[str] = []
    if isinstance(raw_aliases, str):
        aliases.append(raw_aliases)
    elif isinstance(raw_aliases, Sequence):
        aliases.extend(str(alias) for alias in raw_aliases if alias)

    # build_project_context 会把完整 GitHub API 样本放在 github_api_repo_sample。
    repo_sample = repo_context.get("github_api_repo_sample")
    if isinstance(repo_sample, Mapping):
        for key in ("full_name", "description", "homepage"):
            value = repo_sample.get(key)
            if value:
                aliases.append(str(value))

    metrics = repo_context.get("metrics")
    candidate_urls: list[str] = []
    if isinstance(metrics, Mapping) and metrics.get("homepage"):
        candidate_urls.append(str(metrics["homepage"]))
    for url in repo_context.get("evidence_source_urls") or []:
        if url:
            candidate_urls.append(str(url))

    deduped_aliases: list[str] = []
    seen_aliases: set[str] = set()
    for alias in aliases:
        clean = _compact_text(alias)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen_aliases:
            continue
        seen_aliases.add(key)
        deduped_aliases.append(clean)
    return str(project_name) if project_name else None, str(repo_url) if repo_url else None, deduped_aliases, candidate_urls


def _dedupe_signals(signals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for signal in signals:
        url = str(signal.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(dict(signal))
    return deduped


def _platform_summary(signals: Sequence[Mapping[str, Any]], *, error: str | None = None) -> dict[str, Any]:
    non_fallback = [signal for signal in signals if signal.get("source_type") != "fallback"]
    status = "failed" if error else "success" if non_fallback else "empty"
    return {
        "status": status,
        "signal_count": len(signals),
        "usable_signal_count": len(non_fallback),
        "error": error,
    }


def _signal_relevance_partition(
    signals: Sequence[Mapping[str, Any]],
    *,
    project_name: str | None,
    repo_url: str | None,
    aliases: Sequence[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把强相关信号和同名噪声分开。

    设计取舍：宁愿少写社区反馈，也不要把 `impeccable` 这类普通英文词的同名讨论
    当成项目热度。fallback 信号仅用于调试，不进入可用信号。
    """

    owner, repo_name = _repo_owner_name(repo_url)
    strong_terms: list[str] = []
    weak_terms: list[str] = []

    if owner and repo_name:
        strong_terms.extend(
            [
                f"{owner}/{repo_name}",
                f"github.com/{owner}/{repo_name}",
                f"https://github.com/{owner}/{repo_name}",
            ]
        )
        weak_terms.extend([repo_name, owner])
    if project_name:
        if "/" in project_name or "github.com/" in project_name.casefold():
            strong_terms.append(project_name)
        else:
            weak_terms.append(project_name)
    for alias in aliases or []:
        clean = _compact_text(alias)
        if not clean:
            continue
        if "/" in clean or "github.com/" in clean.casefold() or len(clean.split()) >= 2:
            strong_terms.append(clean)
        else:
            weak_terms.append(clean)

    strong_terms = list(dict.fromkeys(term.casefold() for term in strong_terms if term))
    weak_terms = list(dict.fromkeys(term.casefold() for term in weak_terms if term))
    project_context_markers = ("github", "open source", "开源", "repo", "repository", "package", "npm", "pip", "docs")

    usable: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for signal in signals:
        item = dict(signal)
        if item.get("source_type") == "fallback":
            discarded.append({**item, "discard_reason": "fallback_debug_signal"})
            continue
        text = " ".join(
            str(item.get(key) or "")
            for key in ("title", "url", "quote_or_excerpt", "platform", "source_type")
        ).casefold()
        if any(term in text for term in strong_terms):
            usable.append(item)
            continue
        if any(term in text for term in weak_terms) and any(marker in text for marker in project_context_markers):
            usable.append(item)
            continue
        if not strong_terms and any(term in text for term in weak_terms):
            usable.append(item)
            continue
        discarded.append({**item, "discard_reason": "weak_project_relevance"})
    return usable, discarded


async def collect_all_community_signals(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    candidate_urls: Sequence[str] | None = None,
    max_results_per_platform: int = 6,
    timeout_seconds: float = 12.0,
    linux_do_cookie_header: str | None = None,
    include_fallback_signals: bool = False,
) -> dict[str, Any]:
    """统一采集多平台社区/传播信号。

    返回结构会保留每个平台原始 signals，方便前端调试各爬虫输出；同时提供扁平化
    `signals` 供摘要 AI 使用。
    """

    platforms: dict[str, dict[str, Any]] = {}
    limitations: list[str] = []

    async def run_source(
        name: str,
        coro: Any,
        *,
        extra_timeout_seconds: float = 2.0,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        try:
            result = await asyncio.wait_for(coro, timeout=max(3.0, timeout_seconds + extra_timeout_seconds))
            return name, result if isinstance(result, list) else [], None
        except Exception as exc:
            logger.debug("community signal source failed: %s", name, exc_info=True)
            return name, [], f"{type(exc).__name__}: {str(exc)[:500]}"

    tasks = [
        run_source(
            "hacker_news",
            collect_hacker_news_signals(
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
                max_results=max_results_per_platform,
                timeout_seconds=timeout_seconds,
            ),
        ),
        run_source(
            "reddit",
            collect_reddit_community_signals(
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
                max_results=max_results_per_platform,
                search_limit=max(1, max_results_per_platform),
                timeout_seconds=timeout_seconds,
                include_fallback_signal=include_fallback_signals,
            ),
            extra_timeout_seconds=45.0,
        ),
        run_source(
            "linux_do",
            collect_linux_do_signals(
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
                max_results=max_results_per_platform,
                search_limit=max(1, max_results_per_platform),
                timeout_seconds=timeout_seconds,
                cookie_header=linux_do_cookie_header,
            ),
        ),
        run_source(
            "product_hunt_blogs_newsletters",
            collect_community_signals(
                project_name=project_name,
                repo_url=repo_url,
                aliases=aliases,
                candidate_urls=candidate_urls,
                max_results=max_results_per_platform,
                search_limit=max(4, max_results_per_platform * 2),
                timeout_seconds=timeout_seconds,
            ),
        ),
    ]

    all_signals: list[dict[str, Any]] = []
    for name, signals, error in await asyncio.gather(*tasks):
        usable_signals, discarded_signals = _signal_relevance_partition(
            signals,
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
        )
        platforms[name] = {
            "summary": {
                **_platform_summary(usable_signals, error=error),
                "raw_signal_count": len(signals),
                "discarded_signal_count": len(discarded_signals),
            },
            "signals": usable_signals,
            "raw_signals": signals,
            "discarded_signals": discarded_signals[:10],
        }
        all_signals.extend(usable_signals)
        if error:
            limitations.append(f"{name} 抓取失败：{error}")
        elif not [signal for signal in usable_signals if signal.get("source_type") != "fallback"]:
            limitations.append(f"{name} 暂无可用命中")
        if discarded_signals:
            limitations.append(f"{name} 过滤了 {len(discarded_signals)} 条弱相关/调试信号")

    return {
        "version": ALL_COMMUNITY_SIGNAL_EXPANSION_VERSION,
        "expanded_at": _utc_now_iso(),
        "project": {
            "project_name": project_name,
            "repo_url": repo_url,
            "aliases": list(aliases or []),
        },
        "platforms": platforms,
        "signals": _dedupe_signals(all_signals),
        "limitations": limitations,
    }


async def collect_all_community_signals_for_repo_context(
    repo_context: Mapping[str, Any],
    *,
    max_results_per_platform: int = 6,
    timeout_seconds: float = 12.0,
    linux_do_cookie_header: str | None = None,
    include_fallback_signals: bool = False,
) -> dict[str, Any]:
    """字典上下文入口，方便主 GitHub 扩展服务直接调用。"""

    project_name, repo_url, aliases, candidate_urls = _extract_repo_context(repo_context)
    return await collect_all_community_signals(
        project_name=project_name,
        repo_url=repo_url,
        aliases=aliases,
        candidate_urls=candidate_urls,
        max_results_per_platform=max_results_per_platform,
        timeout_seconds=timeout_seconds,
        linux_do_cookie_header=linux_do_cookie_header,
        include_fallback_signals=include_fallback_signals,
    )


def collect_all_community_signals_sync(
    project_name: str | None = None,
    repo_url: str | None = None,
    aliases: Sequence[str] | None = None,
    *,
    candidate_urls: Sequence[str] | None = None,
    max_results_per_platform: int = 6,
    timeout_seconds: float = 12.0,
    linux_do_cookie_header: str | None = None,
    include_fallback_signals: bool = False,
) -> dict[str, Any]:
    """同步聚合入口，供脚本调试使用。"""

    return asyncio.run(
        collect_all_community_signals(
            project_name=project_name,
            repo_url=repo_url,
            aliases=aliases,
            candidate_urls=candidate_urls,
            max_results_per_platform=max_results_per_platform,
            timeout_seconds=timeout_seconds,
            linux_do_cookie_header=linux_do_cookie_header,
            include_fallback_signals=include_fallback_signals,
        )
    )


async def expand_community_signals(
    project_context: Mapping[str, Any],
    repo_ingest_context: Mapping[str, Any] | None = None,
    *,
    max_results_per_platform: int = 6,
    timeout_seconds: float = 12.0,
    linux_do_cookie_header: str | None = None,
    include_fallback_signals: bool = False,
) -> dict[str, Any]:
    """主服务语义入口：基于 GitHub 项目上下文扩展社区/传播信号。"""

    merged_context = dict(project_context)
    if repo_ingest_context:
        repo = repo_ingest_context.get("repo") if isinstance(repo_ingest_context, Mapping) else None
        repo_url = repo_ingest_context.get("repo_url") if isinstance(repo_ingest_context, Mapping) else None
        if repo and "full_name" not in merged_context:
            merged_context["full_name"] = repo
        if repo_url and "repo_url" not in merged_context:
            merged_context["repo_url"] = repo_url

    return await collect_all_community_signals_for_repo_context(
        merged_context,
        max_results_per_platform=max_results_per_platform,
        timeout_seconds=timeout_seconds,
        linux_do_cookie_header=linux_do_cookie_header,
        include_fallback_signals=include_fallback_signals,
    )


__all__ = [
    "COMMUNITY_SIGNAL_EXPANSION_VERSION",
    "HACKER_NEWS_SIGNAL_EXPANSION_VERSION",
    "REDDIT_SIGNAL_EXPANSION_VERSION",
    "LINUX_DO_SIGNAL_EXPANSION_VERSION",
    "ALL_COMMUNITY_SIGNAL_EXPANSION_VERSION",
    "CommunitySignalCandidate",
    "GitHubProjectContext",
    "HackerNewsCommunitySignalCollector",
    "HackerNewsSearchQuery",
    "build_reddit_search_queries",
    "build_hacker_news_search_queries",
    "collect_all_community_signals",
    "collect_all_community_signals_for_repo_context",
    "collect_all_community_signals_sync",
    "collect_community_signals",
    "collect_community_signals_for_repo_context",
    "collect_community_signals_sync",
    "collect_reddit_community_signals",
    "collect_reddit_community_signals_for_repo_context",
    "collect_reddit_community_signals_sync",
    "collect_hacker_news_signals",
    "collect_hacker_news_signals_for_repo_context",
    "collect_hacker_news_signals_sync",
    "expand_community_signals",
    "collect_linux_do_signals",
    "collect_linux_do_signals_for_repo_context",
    "collect_linux_do_signals_sync",
    "discover_community_signal_candidates",
]
