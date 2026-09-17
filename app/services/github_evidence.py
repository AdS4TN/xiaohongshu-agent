from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SourceItemModel

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"
GITHUB_EVIDENCE_VERSION = "github_repo_evidence_v2"
GITHUB_EVIDENCE_SOURCES = ("github_trending_daily", "github")

_LEGACY_README_SIGNAL_KEYS = (
    "has_demo_url",
    "has_docker",
    "has_cli",
    "has_webui",
    "has_api",
    "has_examples",
    "has_install_guide",
    "has_community_link",
)

_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubApiStop(RuntimeError):
    """遇到 GitHub 403/429 时停止后续请求，避免继续消耗 API 配额。"""

    def __init__(self, status_code: int, url: str, message: str | None = None) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(message or f"GitHub API stopped by HTTP {status_code}: {url}")


class GitHubRepoFetchError(RuntimeError):
    """单个仓库请求失败；调用方应记录并继续下一个仓库。"""

    def __init__(self, status_code: int, url: str, message: str | None = None) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(message or f"GitHub repo evidence fetch failed: HTTP {status_code}: {url}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def repo_full_name_from_item(item: SourceItemModel) -> str | None:
    """从 SourceItemModel 中提取 owner/repo，优先使用 source_item_id。"""
    source_item_id = (item.source_item_id or "").strip().strip("/")
    if _FULL_NAME_RE.match(source_item_id):
        return source_item_id

    url = (item.url or "").strip()
    if not url:
        return None

    parsed = urlparse(urljoin(GITHUB_WEB_BASE, url))
    if parsed.netloc and "github.com" not in parsed.netloc.lower():
        return None

    path = unquote(parsed.path).strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None

    full_name = f"{parts[0]}/{parts[1]}"
    return full_name if _FULL_NAME_RE.match(full_name) else None


def _repo_metrics(repo: dict[str, Any]) -> dict[str, Any]:
    license_info = repo.get("license") or {}
    if isinstance(license_info, dict):
        license_key = license_info.get("spdx_id") or license_info.get("key") or license_info.get("name")
    else:
        license_key = None

    return {
        "repo_created_at": repo.get("created_at"),
        "repo_updated_at": repo.get("updated_at"),
        "repo_pushed_at": repo.get("pushed_at"),
        "default_branch": repo.get("default_branch"),
        "homepage": repo.get("homepage"),
        "repo_description": repo.get("description"),
        "topics": repo.get("topics") or [],
        "license": license_key,
        "is_fork": bool(repo.get("fork")),
        "is_archived": bool(repo.get("archived")),
        "is_template": bool(repo.get("is_template")),
        "mirror_url": repo.get("mirror_url"),
        "watchers": repo.get("watchers_count"),
        "subscribers": repo.get("subscribers_count"),
        "open_issues": repo.get("open_issues_count"),
        "network_count": repo.get("network_count"),
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "primary_language_api": repo.get("language"),
    }


def _release_sample(releases: list[Any]) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for release in releases[:5]:
        if not isinstance(release, dict):
            continue
        sample.append(
            {
                "id": release.get("id"),
                "tag_name": release.get("tag_name"),
                "name": release.get("name"),
                "html_url": release.get("html_url"),
                "draft": release.get("draft"),
                "prerelease": release.get("prerelease"),
                "created_at": release.get("created_at"),
                "published_at": release.get("published_at"),
            }
        )
    return sample


def _latest_commit_sample(commit: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(commit, dict):
        return {}
    commit_payload = commit.get("commit") or {}
    author = commit_payload.get("author") or {}
    committer = commit_payload.get("committer") or {}
    message = str(commit_payload.get("message") or "")
    return {
        "sha": commit.get("sha"),
        "html_url": commit.get("html_url"),
        "commit": {
            "author": {"name": author.get("name"), "date": author.get("date")},
            "committer": {"name": committer.get("name"), "date": committer.get("date")},
            "message": message[:500],
        },
    }


def _decode_readme(readme_payload: dict[str, Any] | None) -> str:
    if not isinstance(readme_payload, dict):
        return ""
    content = readme_payload.get("content")
    if not content:
        return ""
    encoding = str(readme_payload.get("encoding") or "").lower()
    if encoding and encoding != "base64":
        return ""

    try:
        compact = "".join(str(content).split())
        return base64.b64decode(compact).decode("utf-8", errors="replace")
    except Exception:
        logger.debug("README base64 decode failed", exc_info=True)
        return ""


def _readme_meta(readme_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(readme_payload, dict):
        return {}
    return {
        "name": readme_payload.get("name"),
        "path": readme_payload.get("path"),
        "html_url": readme_payload.get("html_url"),
        "download_url": readme_payload.get("download_url"),
        "encoding": readme_payload.get("encoding"),
        "size": readme_payload.get("size"),
    }


def _latest_release_metrics(releases: list[Any]) -> dict[str, Any]:
    latest = next((release for release in releases if isinstance(release, dict)), None)
    if not latest:
        return {
            "latest_release_name": None,
            "latest_release_at": None,
            "release_count_sample": 0,
        }
    return {
        "latest_release_name": latest.get("name") or latest.get("tag_name"),
        "latest_release_at": latest.get("published_at") or latest.get("created_at"),
        "release_count_sample": len([release for release in releases[:5] if isinstance(release, dict)]),
    }


def _latest_commit_at(commit: dict[str, Any] | None) -> str | None:
    if not isinstance(commit, dict):
        return None
    commit_payload = commit.get("commit") or {}
    committer = commit_payload.get("committer") or {}
    author = commit_payload.get("author") or {}
    return committer.get("date") or author.get("date")


def _evidence_source_urls(
    full_name: str,
    repo: dict[str, Any],
    readme_payload: dict[str, Any] | None,
    releases: list[Any],
    latest_commit: dict[str, Any] | None,
) -> list[str]:
    urls: list[str] = []
    for candidate in (
        repo.get("html_url") if isinstance(repo, dict) else None,
        _readme_meta(readme_payload).get("html_url"),
        next((release.get("html_url") for release in releases if isinstance(release, dict) and release.get("html_url")), None),
        latest_commit.get("html_url") if isinstance(latest_commit, dict) else None,
        f"{GITHUB_WEB_BASE}/{full_name}",
    ):
        if candidate and candidate not in urls:
            urls.append(str(candidate))
    return urls


def build_evidence_raw_text(
    *,
    full_name: str,
    repo: dict[str, Any],
    languages: dict[str, Any],
    releases: list[Any],
    latest_commit: dict[str, Any] | None,
    readme_text: str,
    original_raw_text: str | None = None,
) -> str:
    description = repo.get("description") or ""
    topics = repo.get("topics") or []
    release_metrics = _latest_release_metrics(releases)
    latest_commit_at = _latest_commit_at(latest_commit)
    license_info = repo.get("license") or {}
    license_key = license_info.get("spdx_id") or license_info.get("key") if isinstance(license_info, dict) else None

    facts = [
        f"full_name: {full_name}",
        f"description: {description}",
        f"topics: {', '.join(str(topic) for topic in topics)}",
        f"homepage: {repo.get('homepage') or ''}",
        f"language: {repo.get('language') or ''}",
        f"languages: {json.dumps(languages or {}, ensure_ascii=False)}",
        f"license: {license_key or ''}",
        f"stars: {repo.get('stargazers_count')}",
        f"forks: {repo.get('forks_count')}",
        f"open_issues: {repo.get('open_issues_count')}",
        f"default_branch: {repo.get('default_branch') or ''}",
        f"created_at: {repo.get('created_at') or ''}",
        f"pushed_at: {repo.get('pushed_at') or ''}",
        f"latest_release: {release_metrics.get('latest_release_name') or ''} {release_metrics.get('latest_release_at') or ''}",
        f"latest_commit_at: {latest_commit_at or ''}",
    ]

    sections = [
        "GitHub 仓库证据",
        "\n".join(facts),
    ]
    if original_raw_text:
        sections.append(f"原始采集文本:\n{original_raw_text.strip()}")
    if readme_text:
        sections.append(f"README:\n{readme_text.strip()}")
    return "\n\n".join(section for section in sections if section).strip()


async def _get_json(client: httpx.AsyncClient, url: str, *, allow_404: bool = False, **kwargs: Any) -> Any:
    response = await client.get(url, **kwargs)
    if response.status_code in {403, 429}:
        raise GitHubApiStop(response.status_code, url)
    if response.status_code == 404 and allow_404:
        return None
    if response.status_code >= 400:
        raise GitHubRepoFetchError(response.status_code, url)
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise GitHubRepoFetchError(response.status_code, url, f"GitHub API JSON decode failed: {url}") from exc


async def fetch_repo_evidence(client: httpx.AsyncClient, full_name: str) -> dict[str, Any]:
    """按仓库 full_name 拉取 GitHub REST API 证据。"""
    safe_full_name = "/".join(part.strip("/") for part in full_name.split("/", 1))
    repo_url = f"{GITHUB_API_BASE}/repos/{safe_full_name}"

    repo = await _get_json(client, repo_url)
    if not isinstance(repo, dict):
        raise GitHubRepoFetchError(200, repo_url, f"GitHub repo payload is not object: {safe_full_name}")

    readme_payload = await _get_json(client, f"{repo_url}/readme", allow_404=True)
    languages = await _get_json(client, f"{repo_url}/languages", allow_404=False)
    releases = await _get_json(client, f"{repo_url}/releases", allow_404=True, params={"per_page": 5})
    commits = await _get_json(client, f"{repo_url}/commits", allow_404=True, params={"per_page": 1})

    if not isinstance(languages, dict):
        languages = {}
    if not isinstance(releases, list):
        releases = []
    latest_commit = commits[0] if isinstance(commits, list) and commits else None

    readme_text = _decode_readme(readme_payload)

    metrics = _repo_metrics(repo)
    metrics.update(
        {
            "has_readme": isinstance(readme_payload, dict),
            "readme_chars": len(readme_text),
            "readme_truncated": False,
            "languages": languages,
            **_latest_release_metrics(releases),
            "latest_commit_at": _latest_commit_at(latest_commit),
            "evidence_enriched_at": _utc_now_iso(),
            "evidence_version": GITHUB_EVIDENCE_VERSION,
        }
    )

    raw_payload = {
        "github_api_repo": repo,
        "github_api_readme": _readme_meta(readme_payload),
        "github_api_languages": languages,
        "github_api_releases_sample": _release_sample(releases),
        "github_api_latest_commit": _latest_commit_sample(latest_commit),
        "readme_text": readme_text,
        "evidence_source_urls": _evidence_source_urls(safe_full_name, repo, readme_payload, releases, latest_commit),
    }

    return {
        "repo": repo,
        "languages": languages,
        "releases": releases,
        "latest_commit": latest_commit,
        "readme_text": readme_text,
        "metrics": metrics,
        "raw_payload": raw_payload,
    }


def _select_github_items(db: Session, *, limit: int, hours: int) -> list[SourceItemModel]:
    stmt = select(SourceItemModel).where(SourceItemModel.source.in_(GITHUB_EVIDENCE_SOURCES))
    if hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        stmt = stmt.where(
            or_(
                SourceItemModel.created_at >= cutoff,
                SourceItemModel.updated_at >= cutoff,
                SourceItemModel.published_at >= cutoff,
            )
        )
    stmt = stmt.order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at)).limit(limit)
    return list(db.execute(stmt).scalars().all())


async def enrich_github_source_items(
    db: Session,
    *,
    limit: int = 50,
    hours: int = 72,
    force: bool = False,
) -> dict[str, Any]:
    """为最近 GitHub 来源 SourceItem 补充仓库 README/API 证据并写回原记录。"""
    stats: dict[str, Any] = {
        "selected": 0,
        "enriched": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "stopped": False,
    }

    if limit <= 0:
        return stats

    items = _select_github_items(db, limit=limit, hours=hours)
    stats["selected"] = len(items)
    if not items:
        return stats

    settings = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AI-Radar-GitHub-Evidence/1.0",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    timeout = httpx.Timeout(settings.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for item in items:
            full_name = repo_full_name_from_item(item)
            if not full_name:
                stats["skipped"] += 1
                stats["errors"].append({"id": item.id, "source": item.source, "error": "missing_repo_full_name"})
                continue

            metrics = _safe_json_loads(item.metrics_json, {})
            if not isinstance(metrics, dict):
                metrics = {}
            if not force and metrics.get("evidence_version") == GITHUB_EVIDENCE_VERSION:
                stats["skipped"] += 1
                continue

            try:
                evidence = await fetch_repo_evidence(client, full_name)
            except GitHubApiStop as exc:
                stats["failed"] += 1
                stats["stopped"] = True
                stats["stop_reason"] = f"http_{exc.status_code}"
                stats["errors"].append(
                    {
                        "id": item.id,
                        "source": item.source,
                        "repo": full_name,
                        "status_code": exc.status_code,
                        "error": "github_api_stopped",
                    }
                )
                logger.warning("GitHub 证据增强停止：repo=%s status=%s", full_name, exc.status_code)
                break
            except Exception as exc:
                stats["failed"] += 1
                stats["errors"].append(
                    {
                        "id": item.id,
                        "source": item.source,
                        "repo": full_name,
                        "error": str(exc)[:500],
                    }
                )
                logger.warning("GitHub 仓库证据增强失败：repo=%s error=%s", full_name, exc)
                continue

            raw_payload = _safe_json_loads(item.raw_payload_json, {})
            if not isinstance(raw_payload, dict):
                raw_payload = {}

            merged_metrics = {**metrics, **evidence["metrics"]}
            for legacy_key in _LEGACY_README_SIGNAL_KEYS:
                merged_metrics.pop(legacy_key, None)
            merged_raw_payload = {**raw_payload, **evidence["raw_payload"]}
            if merged_raw_payload.get("readme_text"):
                merged_raw_payload.pop("readme_excerpt", None)
            repo = evidence["repo"]
            repo_description = repo.get("description") if isinstance(repo, dict) else None

            item.summary = repo_description or item.summary
            item.raw_text = build_evidence_raw_text(
                full_name=full_name,
                repo=repo,
                languages=evidence["languages"],
                releases=evidence["releases"],
                latest_commit=evidence["latest_commit"],
                readme_text=evidence["readme_text"],
                original_raw_text=item.raw_text,
            )
            item.metrics_json = _dump_json(merged_metrics)
            item.raw_payload_json = _dump_json(merged_raw_payload)
            db.add(item)
            db.commit()
            stats["enriched"] += 1
            logger.info("GitHub 证据增强完成：repo=%s source=%s id=%s", full_name, item.source, item.id)

    return stats
