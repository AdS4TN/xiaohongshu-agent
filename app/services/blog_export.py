from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import HotspotModel, HotspotSourceModel, ResearchReportModel
from app.services.blog_analysis import analysis_data_for_hotspot


CONTENT_TYPE_LABELS = {
    "ai_tool_review": "AI 工具测评",
    "github_project": "开源项目",
    "paper_explainer": "论文解读",
    "product_discovery": "产品发现",
    "tech_news": "科技新闻",
}


def _settings_paths() -> tuple[Path, Path | None]:
    settings = get_settings()
    public_dir = Path(settings.public_export_dir)
    blog_dir = Path(settings.blog_export_dir) if settings.blog_export_dir else None
    return public_dir, blog_dir


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def _now_local() -> datetime:
    return datetime.now(_tz())


def _to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_tz())


def _iso(value: datetime | None) -> str | None:
    local_value = _to_local(value)
    return local_value.isoformat() if local_value else None


def _read_json(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def _slugify_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:56].strip("-") or "hotspot"


def slug_for_hotspot(hotspot: HotspotModel) -> str:
    digest = hashlib.sha1(f"{hotspot.id}:{hotspot.title}".encode("utf-8")).hexdigest()[:8]
    return f"{_slugify_title(hotspot.title)}-{digest}"


def report_date_for_hotspot(hotspot: HotspotModel) -> str:
    local_created = _to_local(hotspot.created_at) or _now_local()
    return local_created.date().isoformat()


def _source_dict(link: HotspotSourceModel) -> dict[str, Any]:
    item = link.source_item
    return {
        "source": item.source,
        "source_label": item.source,
        "title": item.title,
        "url": item.url,
        "author": item.author,
        "summary": item.summary,
        "published_at": _iso(item.published_at),
        "metrics": _read_json(item.metrics_json, {}),
    }


def _latest_success_report(hotspot: HotspotModel) -> ResearchReportModel | None:
    success_reports = [r for r in hotspot.research_reports if r.status == "success"]
    if not success_reports:
        return None
    return sorted(success_reports, key=lambda r: r.created_at, reverse=True)[0]


def _latest_running_report(hotspot: HotspotModel) -> ResearchReportModel | None:
    running_reports = [r for r in hotspot.research_reports if r.status == "running"]
    if not running_reports:
        return None
    return sorted(running_reports, key=lambda r: r.created_at, reverse=True)[0]


def _card_summary(hotspot: HotspotModel) -> str:
    text = (hotspot.summary or "").strip()
    if text:
        return text[:120]
    return (hotspot.why_it_matters or hotspot.title).strip()[:120]


def _detail_base(hotspot: HotspotModel, *, report_date: str | None = None) -> dict[str, Any]:
    report_date = report_date or report_date_for_hotspot(hotspot)
    slug = slug_for_hotspot(hotspot)
    sources = [_source_dict(link) for link in hotspot.sources]
    source_tags = sorted({s["source"] for s in sources})
    content_type_label = CONTENT_TYPE_LABELS.get(hotspot.content_type, hotspot.content_type)
    detail_path = f"{get_settings().blog_detail_base_path.rstrip('/')}/{report_date}/{slug}"
    item_json_path = f"items/{report_date}/{slug}.json"
    success_report = _latest_success_report(hotspot)
    running_report = _latest_running_report(hotspot)
    analysis = analysis_data_for_hotspot(hotspot) or {}
    card_analysis = analysis.get("card") if isinstance(analysis.get("card"), dict) else {}
    article_analysis = analysis.get("article") if isinstance(analysis.get("article"), dict) else {}

    key_facts = []
    for source in sources[:5]:
        metric_parts = []
        metrics = source.get("metrics") or {}
        for key in ("stars", "stars_today", "likes", "downloads", "score", "descendants", "star_delta_24h"):
            if metrics.get(key) not in (None, "", 0):
                metric_parts.append(f"{key}={metrics.get(key)}")
        suffix = f"（{', '.join(metric_parts)}）" if metric_parts else ""
        key_facts.append(f"{source['source']}：{source['title']}{suffix}")

    title = card_analysis.get("title") or hotspot.title
    summary = card_analysis.get("summary") or _card_summary(hotspot)
    type_label = card_analysis.get("type_label") or CONTENT_TYPE_LABELS.get(hotspot.content_type, hotspot.content_type)
    article_title = article_analysis.get("title") or title
    article_content = article_analysis.get("content") or (
        f"{title} 最近被多条公开来源提到。{summary} "
        f"从公开来源看，它主要来自 {source_tags[0] if source_tags else '公开来源'} 等渠道，"
        "相关公开信号显示它具备一定热度，但仍应结合原始来源、发布时间和上下文理解。"
    )

    return {
        "id": hotspot.id,
        "slug": slug,
        "report_date": report_date,
        "title": title,
        "original_title": hotspot.title,
        "summary": summary,
        "importance_reason": card_analysis.get("importance_reason"),
        "importance_score": round(float(hotspot.score or 0), 2),
        "source_tags": source_tags,
        "type": hotspot.content_type,
        "type_label": type_label or content_type_label,
        "analysis_source": "codex" if analysis else "rule_fallback",
        "detail_path": detail_path,
        "item_json_path": item_json_path,
        "created_at": _iso(hotspot.created_at),
        "updated_at": _iso(hotspot.updated_at),
        "detail": {
            "article_title": article_title,
            "article_content": article_content,
            "related_sources": sources,
            # 内部字段：保留给后续小红书内容 agent 使用，博客展示层默认不要渲染。
            "internal_xiaohongshu_angle": hotspot.xiaohongshu_angle,
            "risk": hotspot.risk,
        },
        "deep_research": {
            "status": "success" if success_report else ("running" if running_report else "not_started"),
            "report_id": success_report.id if success_report else (running_report.id if running_report else None),
            "model": success_report.model if success_report else (running_report.model if running_report else None),
            "generated_at": _iso(success_report.finished_at) if success_report else None,
            "markdown": success_report.report_markdown if success_report else None,
            "sources": _read_json(success_report.sources_json, []) if success_report else [],
        },
    }


def _impact_text(hotspot: HotspotModel) -> str:
    if hotspot.content_type == "github_project":
        return "可能影响开发者工具链、开源 AI 应用形态以及可被普通用户体验的新产品原型。"
    if hotspot.content_type == "ai_tool_review":
        return "可能影响 AI 工具使用者的工作流选择，尤其适合从实际体验、适用人群和替代成本角度观察。"
    if hotspot.content_type == "paper_explainer":
        return "可能代表模型能力、Agent、多模态或基础设施方向的新研究信号，但需要区分研究进展和可用产品。"
    if hotspot.content_type == "product_discovery":
        return "可能提供新的模型、Demo 或产品能力线索，适合进一步验证真实可用性和使用门槛。"
    return "可能反映 AI/科技圈的新讨论方向，适合结合多源事实判断其持续性。"


def _objective_analysis(hotspot: HotspotModel, sources: list[dict[str, Any]]) -> str:
    source_names = "、".join(sorted({s["source"] for s in sources})) or "公开来源"
    risk = hotspot.risk or "仍需人工核验来源真实性、发布时间和实际可用性。"
    return (
        f"该条目主要来自 {source_names}，当前评分为 {round(float(hotspot.score or 0), 2)}。"
        f"从公开信号看，它具备一定关注度或新鲜度；但在进入更长篇分析前，仍应核验原始链接、关键指标和上下文。"
        f"{risk}"
    )


def _query_hotspots_for_export(db: Session, target_date: date | None = None, limit: int | None = None) -> list[HotspotModel]:
    settings = get_settings()
    limit = limit or settings.daily_card_limit
    stmt = (
        select(HotspotModel)
        .options(
            selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item),
            selectinload(HotspotModel.research_reports),
            selectinload(HotspotModel.blog_analyses),
        )
        .order_by(desc(HotspotModel.score), desc(HotspotModel.created_at))
    )
    if target_date is None:
        since_local = _now_local() - timedelta(hours=settings.daily_export_lookback_hours)
        since_utc = since_local.astimezone(timezone.utc)
        stmt = stmt.where(HotspotModel.created_at >= since_utc)
    else:
        start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=_tz())
        end_local = start_local + timedelta(days=1)
        stmt = stmt.where(
            HotspotModel.created_at >= start_local.astimezone(timezone.utc),
            HotspotModel.created_at < end_local.astimezone(timezone.utc),
        )
    return list(db.execute(stmt.limit(limit)).scalars().all())


def build_export_payload(db: Session, target_date: date | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    generated_at = _now_local()
    export_date = target_date or generated_at.date()
    hotspots = _query_hotspots_for_export(db, target_date=target_date, limit=limit)
    items = [_detail_base(h, report_date=export_date.isoformat()) for h in hotspots]
    cards = [
        {
            "id": item["id"],
            "slug": item["slug"],
            "report_date": item["report_date"],
            "title": item["title"],
            "summary": item["summary"],
            "importance_score": item["importance_score"],
            "source_tags": item["source_tags"],
            "type": item["type"],
            "type_label": item["type_label"],
            "analysis_source": item["analysis_source"],
            "importance_reason": item["importance_reason"],
            "detail_path": item["detail_path"],
            "item_json_path": item["item_json_path"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "deep_research_status": item["deep_research"]["status"],
        }
        for item in items
    ]
    return {
        "schema_version": "ai-hotspots.v1",
        "timezone": settings.timezone,
        "date": export_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "card_limit": limit or settings.daily_card_limit,
        "cards": cards,
        "items": items,
    }


async def build_export_payload_with_llm(
    db: Session,
    target_date: date | None = None,
    limit: int | None = None,
    *,
    force_analysis: bool = False,
) -> dict[str, Any]:
    """先用 Codex 写作接口预生成 A/B 层缓存，再构建导出 JSON。"""

    from app.services.blog_analysis import ensure_blog_analyses_for_hotspots

    hotspots = _query_hotspots_for_export(db, target_date=target_date, limit=limit)
    await ensure_blog_analyses_for_hotspots(db, hotspots, force=force_analysis)
    # 清理 SQLAlchemy identity map，避免 relationship 仍保留旧缓存导致导出 rule_fallback。
    db.expire_all()
    # 重新查询一次，确保 relationship 中包含刚写入的 blog_analyses。
    return build_export_payload(db, target_date=target_date, limit=limit)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(path)


def _sync_export_dir(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    # copytree dirs_exist_ok 在 Python 3.8+ 可用。这里同步的是导出产物，不做删除，避免误删博客文件。
    shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def export_blog_hotspots(db: Session, target_date: date | None = None, limit: int | None = None) -> dict[str, Any]:
    payload = build_export_payload(db, target_date=target_date, limit=limit)
    return write_blog_export_payload(payload)


def write_blog_export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    public_dir, blog_dir = _settings_paths()
    export_date = payload["date"]

    latest_payload = {k: v for k, v in payload.items() if k != "items"}
    archive_payload = latest_payload

    _write_json(public_dir / "latest.json", latest_payload)
    _write_json(public_dir / "archive" / f"{export_date}.json", archive_payload)

    item_paths: list[str] = []
    for item in payload["items"]:
        item_path = public_dir / "items" / item["report_date"] / f"{item['slug']}.json"
        _write_json(item_path, item)
        item_paths.append(str(item_path))

    if blog_dir is not None:
        _sync_export_dir(public_dir, blog_dir)

    return {
        "status": "success",
        "date": export_date,
        "timezone": payload["timezone"],
        "cards": len(payload["cards"]),
        "public_export_dir": str(public_dir),
        "blog_export_dir": str(blog_dir) if blog_dir else None,
        "latest_json": str(public_dir / "latest.json"),
        "archive_json": str(public_dir / "archive" / f"{export_date}.json"),
        "item_json_paths": item_paths,
    }


async def export_blog_hotspots_with_llm(
    db: Session,
    target_date: date | None = None,
    limit: int | None = None,
    *,
    force_analysis: bool = False,
) -> dict[str, Any]:
    payload = await build_export_payload_with_llm(db, target_date=target_date, limit=limit, force_analysis=force_analysis)
    return write_blog_export_payload(payload)


def find_hotspot_by_date_slug(db: Session, report_date: str, slug: str) -> HotspotModel | None:
    """按导出 URL 中的日期和 slug 反查热点。

    slug 由标题和热点 id 派生，未持久化到数据库。考虑到每日卡片只有 5-8 条，
    这里按日期窗口取一批候选后在应用层匹配，避免为 V1 增加数据库迁移成本。
    """

    try:
        target = date.fromisoformat(report_date)
    except ValueError:
        return None

    settings = get_settings()
    start_local = datetime.combine(target, datetime.min.time(), tzinfo=_tz()) - timedelta(hours=settings.daily_export_lookback_hours)
    end_local = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=_tz())

    stmt = (
        select(HotspotModel)
        .options(
            selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item),
            selectinload(HotspotModel.research_reports),
            selectinload(HotspotModel.blog_analyses),
        )
        .where(
            HotspotModel.created_at >= start_local.astimezone(timezone.utc),
            HotspotModel.created_at < end_local.astimezone(timezone.utc),
        )
        .order_by(desc(HotspotModel.score), desc(HotspotModel.created_at))
        .limit(200)
    )
    for hotspot in db.execute(stmt).scalars().all():
        if slug_for_hotspot(hotspot) == slug:
            return hotspot
    return None


def exported_item_for_hotspot(hotspot: HotspotModel, *, report_date: str | None = None) -> dict[str, Any]:
    return _detail_base(hotspot, report_date=report_date)
