from __future__ import annotations

import json
from datetime import datetime, timezone

from app.models import SourceItemModel

SOURCE_WEIGHTS = {
    "github": 1.2,
    "github_trending_daily": 1.25,
    "hf_spaces": 1.2,
    "hf_daily_papers": 1.1,
    "hf_models": 1.0,
    "hackernews": 1.0,
    "arxiv": 0.9,
}


def _json(text: str) -> dict:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}


def infer_content_type(item: SourceItemModel) -> str:
    if item.source in {"github", "github_trending_daily"}:
        return "github_project"
    if item.source == "hf_spaces":
        return "ai_tool_review"
    if item.source in {"arxiv", "hf_daily_papers"}:
        return "paper_explainer"
    if item.source == "hf_models":
        return "product_discovery"
    return "tech_news"


def score_item(item: SourceItemModel) -> float:
    metrics = _json(item.metrics_json)
    score = 30.0 * SOURCE_WEIGHTS.get(item.source, 1.0)

    if item.published_at:
        now = datetime.now(timezone.utc)
        published = item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=timezone.utc)
        age_hours = max((now - published).total_seconds() / 3600, 0)
        score += max(0, 25 - age_hours / 4)

    if item.source == "github":
        stars = float(metrics.get("stars") or 0)
        star_delta_24h = float(metrics.get("star_delta_24h") or 0)
        star_delta_72h = float(metrics.get("star_delta_72h") or 0)
        score += min(25, stars ** 0.5)
        score += min(20, star_delta_24h * 0.8)
        score += min(12, star_delta_72h * 0.25)
        if metrics.get("homepage"):
            score += 8
        if item.summary and len(item.summary) > 40:
            score += 5
    elif item.source == "github_trending_daily":
        stars = float(metrics.get("stars") or 0)
        stars_today = float(metrics.get("stars_today") or 0)
        forks = float(metrics.get("forks") or 0)
        rank = int(metrics.get("rank") or 0)
        score += min(22, stars ** 0.5)
        score += min(24, stars_today * 0.7)
        score += min(8, forks ** 0.5 / 2)
        if 0 < rank <= 10:
            score += max(0, 12 - rank)
        if metrics.get("homepage"):
            score += 6
        if metrics.get("language"):
            score += 3
        if item.summary and len(item.summary) > 40:
            score += 5
    elif item.source in {"hf_models", "hf_spaces"}:
        score += min(20, (float(metrics.get("likes") or 0) ** 0.5) * 2)
        score += min(10, (float(metrics.get("downloads") or 0) ** 0.5) / 10)
    elif item.source == "hackernews":
        score += min(25, (float(metrics.get("score") or 0) ** 0.5) * 2)
        score += min(10, (float(metrics.get("descendants") or 0) ** 0.5))
    elif item.source == "hf_daily_papers":
        score += 12

    if infer_content_type(item) in {"ai_tool_review", "github_project", "product_discovery"}:
        score += 8
    return round(min(score, 100), 2)
