from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import date, datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_, select

from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.models import SourceItemModel
from app.scheduler import start_scheduler, stop_scheduler
from app.schemas import HotspotOut, SelectionIn
from app.services.blog_export import (
    build_export_payload,
    export_blog_hotspots,
    export_blog_hotspots_with_llm,
    exported_item_for_hotspot,
    find_hotspot_by_date_slug,
    write_blog_export_payload,
)
from app.services.daily_report import ensure_daily_report, latest_daily_report
from app.services.github_info_expansion import (
    expand_and_save_community_signals,
    expand_and_save_github_project_info,
)
from app.services.hotspot import is_running, latest_source_runs, list_hotspots, run_full_ingest, set_hotspot_status
from app.services.news_notify import notify_news_dashboard
from app.services.research_agent import (
    create_research_report,
    get_research_report,
    latest_research_report,
    latest_success_research_report,
    run_research_report,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="AI Radar V1", version="1.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["json_pretty"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)

_github_info_lock = threading.Lock()
_github_info_running_ids: set[int] = set()
_github_info_failed: dict[int, str] = {}
_github_info_attempts: dict[int, int] = {}
_GITHUB_INFO_MAX_ATTEMPTS = 3


def _safe_load_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_reddit_devvit_token(request: Request) -> None:
    expected = (get_settings().reddit_devvit_shared_token or "").strip()
    if not expected:
        return
    authorization = request.headers.get("authorization") or ""
    bearer = authorization.removeprefix("Bearer").strip() if authorization.lower().startswith("bearer") else ""
    header_token = (request.headers.get("x-devvit-token") or "").strip()
    if bearer == expected or header_token == expected:
        return
    raise HTTPException(status_code=401, detail="invalid reddit devvit token")


def _repo_aliases_for_watchlist(item: SourceItemModel, metrics: dict, raw_payload: dict) -> list[str]:
    aliases: list[str] = []
    if item.source_item_id:
        aliases.append(item.source_item_id)
    if item.title:
        aliases.append(item.title)
    if item.url:
        aliases.append(item.url)
    repo_payload = raw_payload.get("github_api_repo")
    if isinstance(repo_payload, dict):
        for key in ("full_name", "name", "html_url", "description", "homepage"):
            value = repo_payload.get(key)
            if value:
                aliases.append(str(value))
    for value in metrics.get("topics") or []:
        if value:
            aliases.append(str(value))

    seen: set[str] = set()
    deduped: list[str] = []
    for alias in aliases:
        clean = " ".join(str(alias).split()).strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(clean)
    return deduped[:20]


def _normalize_devvit_signal(signal: dict, source_item_id: int) -> dict:
    created_utc = signal.get("created_utc")
    published_at = signal.get("published_at") or signal.get("created_at")
    if not published_at and created_utc is not None:
        try:
            published_at = datetime.fromtimestamp(float(created_utc), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            published_at = None

    score = signal.get("score")
    num_comments = signal.get("num_comments")
    engagement = signal.get("engagement")
    if not engagement:
        engagement_parts = []
        if score is not None:
            engagement_parts.append(f"score={score}")
        if num_comments is not None:
            engagement_parts.append(f"comments={num_comments}")
        engagement = ", ".join(engagement_parts) or "unknown"

    normalized = {
        "platform": "Reddit",
        "source_type": signal.get("source_type") or "post_devvit",
        "url": signal.get("url") or signal.get("permalink") or "",
        "title": signal.get("title") or "unknown",
        "published_at": published_at or "unknown",
        "engagement": engagement,
        "stance": signal.get("stance") or "unknown",
        "quote_or_excerpt": signal.get("quote_or_excerpt") or signal.get("excerpt") or "unknown",
        "topic_tags": signal.get("topic_tags") if isinstance(signal.get("topic_tags"), list) else ["reddit", "devvit"],
        "relevance_reason": signal.get("relevance_reason") or "Reddit Devvit collector 命中项目 watchlist 关键词",
        "source_item_id": source_item_id,
        "received_at": _utc_now_iso(),
    }
    # 保留 Devvit 原始命中定位字段，方便前端调试、去重和后续追溯。
    for key in (
        "project_key",
        "subreddit",
        "post_id",
        "permalink",
        "author",
        "score",
        "num_comments",
        "created_utc",
        "matched_aliases",
    ):
        value = signal.get(key)
        if value is not None:
            normalized[key] = value
    return normalized


def _github_info_attempt_meta_from_raw_payload(raw_payload: dict) -> dict:
    meta = raw_payload.get("github_info_expansion_attempt")
    return meta if isinstance(meta, dict) else {}


def _github_info_attempt_meta(db: Session, item_id: int) -> dict:
    item = db.get(SourceItemModel, item_id)
    if item is None:
        return {}
    raw_payload = _safe_load_json_object(item.raw_payload_json)
    return _github_info_attempt_meta_from_raw_payload(raw_payload)


def _save_github_info_attempt_meta(db: Session, item_id: int, meta: dict) -> None:
    item = db.get(SourceItemModel, item_id)
    if item is None:
        return
    raw_payload = _safe_load_json_object(item.raw_payload_json)
    raw_payload["github_info_expansion_attempt"] = meta
    item.raw_payload_json = json.dumps(raw_payload, ensure_ascii=False, default=str)
    db.add(item)
    db.commit()


def _github_expansion_debug_payload(expansion: dict | None) -> dict | None:
    if not isinstance(expansion, dict):
        return None

    summary = expansion.get("final_summary") if isinstance(expansion.get("final_summary"), dict) else {}
    round1_review = expansion.get("round1_review") if isinstance(expansion.get("round1_review"), dict) else {}
    round2_review = expansion.get("round2_review") if isinstance(expansion.get("round2_review"), dict) else {}
    agent_outputs = expansion.get("agent_outputs") if isinstance(expansion.get("agent_outputs"), dict) else {}

    stages = [
        ("repo_ingest_context", "仓库读取 · repo ingest", expansion.get("repo_ingest_context")),
        ("round1_search", "搜索 AI · 第 1 轮", expansion.get("round1_search")),
        ("round1_review", "审核 AI · 第 1 轮", round1_review),
        ("round2_search", "搜索 AI · 第 2 轮", expansion.get("round2_search")),
        ("round2_review", "审核 AI · 第 2 轮", round2_review),
        ("community_signal_crawlers", "社区信号爬虫 · HN/Reddit/Linux.do", expansion.get("community_signals_context")),
        ("final_summary", "摘要 AI · 最终成稿", summary),
    ]
    debug_stages = []
    for key, label, parsed in stages:
        output = agent_outputs.get(key) if isinstance(agent_outputs.get(key), dict) else {}
        debug_stages.append(
            {
                "key": key,
                "label": label,
                "model": output.get("model"),
                "agent": output.get("agent"),
                "raw_content": output.get("raw_content"),
                "parsed": parsed,
            }
        )

    return {
        "version": expansion.get("version"),
        "expanded_at": expansion.get("expanded_at"),
        "models": expansion.get("models"),
        "is_complete": _github_info_expansion_complete(expansion),
        "summary_parse_error": summary.get("_parse_error"),
        "round1_verdict": round1_review.get("verdict"),
        "round2_verdict": round2_review.get("verdict"),
        "one_liner": summary.get("one_liner"),
        "daily_report_paragraph": summary.get("daily_report_paragraph"),
        "stages": debug_stages,
    }


def _github_info_expansion_complete(expansion: dict | None) -> bool:
    """最终摘要可解析且有正文时，才认为 GitHub 信息扩展完成。"""

    if not isinstance(expansion, dict):
        return False
    summary = expansion.get("final_summary")
    if not isinstance(summary, dict) or summary.get("_parse_error"):
        return False
    return bool(summary.get("one_liner") or summary.get("daily_report_paragraph"))


def _github_info_state_for(item_id: int, has_expansion: bool, attempt_meta: dict | None = None) -> dict:
    attempt_meta = attempt_meta if isinstance(attempt_meta, dict) else {}
    with _github_info_lock:
        running = item_id in _github_info_running_ids
        failed_message = _github_info_failed.get(item_id)
        memory_attempts = _github_info_attempts.get(item_id, 0)
    try:
        persisted_attempts = int(attempt_meta.get("attempt") or 0)
    except (TypeError, ValueError):
        persisted_attempts = 0
    attempts = max(memory_attempts, persisted_attempts)
    if has_expansion:
        status = "done"
        label = "已生成"
    elif running:
        status = "running"
        label = f"AI 探索中（第 {max(1, attempts)} 次）"
    elif failed_message:
        status = "failed"
        label = f"探索失败：{failed_message[:120]}"
    elif attempt_meta.get("status") == "failed":
        status = "failed"
        error_text = str(attempt_meta.get("error_message") or "")[:120]
        label = f"探索失败（已自动重试 {attempts}/{_GITHUB_INFO_MAX_ATTEMPTS}）"
        if error_text:
            label = f"{label}：{error_text}"
    elif attempt_meta.get("status") in {"running", "retrying"}:
        status = "queued"
        label = "上次生成未完成，可重新生成"
    else:
        status = "queued"
        label = "等待自动探索"
    return {
        "status": status,
        "label": label,
        "running": running,
        "error": failed_message or attempt_meta.get("error_message"),
        "attempts": attempts,
        "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
    }


def _github_item_has_expansion(db: Session, item_id: int) -> bool:
    item = db.get(SourceItemModel, item_id)
    if item is None:
        return False
    raw_payload = _safe_load_json_object(item.raw_payload_json)
    return _github_info_expansion_complete(raw_payload.get("github_info_expansion"))


def _run_github_info_expansion_worker(item_id: int) -> None:
    db = SessionLocal()
    try:
        if _github_item_has_expansion(db, item_id):
            return
        last_error: Exception | None = None
        for attempt in range(1, _GITHUB_INFO_MAX_ATTEMPTS + 1):
            with _github_info_lock:
                _github_info_attempts[item_id] = attempt
            _save_github_info_attempt_meta(
                db,
                item_id,
                {
                    "status": "running",
                    "attempt": attempt,
                    "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
                    "started_at": _utc_now_iso(),
                },
            )
            try:
                expansion = asyncio.run(expand_and_save_github_project_info(db, item_id))
                if not _github_info_expansion_complete(expansion):
                    raise RuntimeError("AI 检索已返回，但最终摘要 JSON 解析失败或缺少摘要正文")
                _save_github_info_attempt_meta(
                    db,
                    item_id,
                    {
                        "status": "success",
                        "attempt": attempt,
                        "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
                        "finished_at": _utc_now_iso(),
                    },
                )
                with _github_info_lock:
                    _github_info_failed.pop(item_id, None)
                notify_news_dashboard(
                    "github_trending_daily_enriched",
                    {
                        "source_item_id": item_id,
                        "status": "success",
                        "attempt": attempt,
                    },
                )
                return
            except Exception as exc:
                last_error = exc
                logging.exception("GitHub 信息扩展失败 source_item_id=%s attempt=%s/%s", item_id, attempt, _GITHUB_INFO_MAX_ATTEMPTS)
                _save_github_info_attempt_meta(
                    db,
                    item_id,
                    {
                        "status": "failed" if attempt >= _GITHUB_INFO_MAX_ATTEMPTS else "retrying",
                        "attempt": attempt,
                        "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
                        "error_message": str(exc)[:1000],
                        "finished_at": _utc_now_iso(),
                    },
                )
                with _github_info_lock:
                    _github_info_failed[item_id] = f"第 {attempt}/{_GITHUB_INFO_MAX_ATTEMPTS} 次失败：{str(exc)[:900]}"
                if attempt < _GITHUB_INFO_MAX_ATTEMPTS:
                    time.sleep(min(20, 3 * attempt))
        return
    finally:
        db.close()
        with _github_info_lock:
            _github_info_running_ids.discard(item_id)


def _ensure_github_info_expansion_queued(item_ids: list[int]) -> None:
    """为缺失 AI 摘要的 GitHub 项目启动后台探索。

    为了避免一次打开页面就把所有模型接口打爆，这里最多同时跑 2 个。
    页面会定时刷新状态，完成一个后下一次刷新会继续补齐后面的项目。
    """

    max_concurrent = 2
    db = SessionLocal()
    try:
        for item_id in item_ids:
            with _github_info_lock:
                if item_id in _github_info_running_ids:
                    continue
            meta = _github_info_attempt_meta(db, item_id)
            try:
                attempts = int(meta.get("attempt") or 0)
            except (TypeError, ValueError):
                attempts = 0
            # 自动重试最多三次。到达上限后不再由页面刷新自动触发；
            # 用户仍可点击“重试 AI 检索”手动开启新一轮三次重试。
            if meta.get("status") == "failed" and attempts >= _GITHUB_INFO_MAX_ATTEMPTS:
                continue
            with _github_info_lock:
                if len(_github_info_running_ids) >= max_concurrent:
                    return
                _github_info_running_ids.add(item_id)
            threading.Thread(target=_run_github_info_expansion_worker, args=(item_id,), daemon=True).start()
    finally:
        db.close()


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    start_scheduler()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_scheduler()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    payload = build_export_payload(db)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "payload": payload,
            "cards": payload["cards"],
            "running": is_running(),
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("app/static/favicon.ico")


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("sources.html", {"request": request, "runs": latest_source_runs(db)})


@app.get("/github-daily", response_class=HTMLResponse)
def github_daily_preview(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(SourceItemModel)
        .where(SourceItemModel.source == "github_trending_daily")
        .order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at))
        .limit(200)
    ).scalars().all()

    parsed_rows = []
    latest_snapshot_time: str | None = None
    for item in rows:
        metrics = _safe_load_json_object(item.metrics_json)
        raw_payload = _safe_load_json_object(item.raw_payload_json)
        snapshot_time = str(metrics.get("snapshot_time") or "")
        parsed_rows.append((item, metrics, raw_payload, snapshot_time))
        if snapshot_time and (latest_snapshot_time is None or snapshot_time > latest_snapshot_time):
            latest_snapshot_time = snapshot_time

    # 只展示同一次 GitHub Trending Daily 抓取快照。
    # 之前把最近 50 个历史条目混在一起，再按 metrics.rank 排序；
    # 不同快照都会有 #6/#7/#8/#9，所以页面会出现 6,6,7,7,8,8,9,9。
    if latest_snapshot_time:
        parsed_rows = [row for row in parsed_rows if row[3] == latest_snapshot_time]

    cards = []
    languages: dict[str, int] = {}
    total_stars_today = 0
    enriched_count = 0
    missing_info_item_ids: list[int] = []

    for item, metrics, raw_payload, snapshot_time in parsed_rows:
        github_info_expansion = raw_payload.get("github_info_expansion")
        attempt_meta = _github_info_attempt_meta_from_raw_payload(raw_payload)
        github_info_debug = _github_expansion_debug_payload(github_info_expansion)
        github_community_signals = raw_payload.get("github_community_signals")
        if not isinstance(github_community_signals, dict):
            github_community_signals = None
        info_expansion_complete = bool(github_info_debug and github_info_debug.get("is_complete"))
        if not info_expansion_complete:
            missing_info_item_ids.append(item.id)
        info_state = _github_info_state_for(item.id, info_expansion_complete, attempt_meta)

        language = metrics.get("primary_language_api") or metrics.get("language") or "未知"
        languages[language] = languages.get(language, 0) + 1
        total_stars_today += int(metrics.get("stars_today") or 0)
        if metrics.get("evidence_version"):
            enriched_count += 1

        topics = metrics.get("topics") or []
        if not isinstance(topics, list):
            topics = []

        readme_text = raw_payload.get("readme_text")
        if not isinstance(readme_text, str):
            readme_text = raw_payload.get("readme_excerpt") or ""
        readme_text = readme_text.strip()
        readme_saved_chars = len(readme_text)
        cards.append(
            {
                "id": item.id,
                "source_item_id": item.source_item_id,
                "title": item.title,
                "url": item.url,
                "summary": item.summary,
                "rank": int(metrics.get("rank") or 9999),
                "stars_today": int(metrics.get("stars_today") or 0),
                "stars": int(metrics.get("stars") or 0),
                "forks": int(metrics.get("forks") or 0),
                "language": language,
                "topics": topics[:8],
                "repo_created_at": metrics.get("repo_created_at"),
                "repo_pushed_at": metrics.get("repo_pushed_at"),
                "latest_release_name": metrics.get("latest_release_name"),
                "latest_release_at": metrics.get("latest_release_at"),
                "readme_chars": int(metrics.get("readme_chars") or 0),
                "readme_saved_chars": readme_saved_chars,
                "readme_text": readme_text,
                "evidence_ready": bool(metrics.get("evidence_version")),
                "info_expansion_ready": info_expansion_complete,
                "info_state": info_state,
                "info_expanded_at": github_info_debug.get("expanded_at") if github_info_debug else None,
                "info_round1_verdict": github_info_debug.get("round1_verdict") if github_info_debug else None,
                "info_round2_verdict": github_info_debug.get("round2_verdict") if github_info_debug else None,
                "info_one_liner": github_info_debug.get("one_liner") if github_info_debug else None,
                "info_daily_report_paragraph": github_info_debug.get("daily_report_paragraph") if github_info_debug else None,
                "info_debug": github_info_debug,
                "info_attempt_meta": attempt_meta,
                "info_can_retry": not info_expansion_complete,
                "community_signals": github_community_signals,
                "raw_text_len": len(item.raw_text or ""),
                "snapshot_time": snapshot_time or metrics.get("snapshot_time"),
            }
        )

    cards.sort(key=lambda c: c["rank"])
    missing_info_item_ids = [
        card["id"]
        for card in cards[:25]
        if not card["info_expansion_ready"] and card["info_state"]["status"] != "failed"
    ]
    _ensure_github_info_expansion_queued(missing_info_item_ids)
    for card in cards[:25]:
        if not card["info_expansion_ready"]:
            card["info_state"] = _github_info_state_for(card["id"], False, card.get("info_attempt_meta"))
    language_stats = sorted(languages.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return templates.TemplateResponse(
        "github_daily.html",
        {
            "request": request,
            "cards": cards[:25],
            "total": len(cards),
            "enriched_count": enriched_count,
            "info_ready_count": sum(1 for card in cards[:25] if card["info_expansion_ready"]),
            "info_running_count": sum(1 for card in cards[:25] if card["info_state"]["status"] == "running"),
            "info_pending_count": sum(1 for card in cards[:25] if card["info_state"]["status"] == "queued"),
            "total_stars_today": total_stars_today,
            "language_stats": language_stats,
            "running": is_running(),
        },
    )


@app.get("/selected", response_class=HTMLResponse)
def selected(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("selected.html", {"request": request, "hotspots": list_hotspots(db, status="selected", hours=None)})


@app.get("/research/{report_id}", response_class=HTMLResponse)
def research_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    report = get_research_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="research report not found")
    return templates.TemplateResponse("research.html", {"request": request, "report": report})


@app.get("/ai-hotspots/{report_date}/{slug}", response_class=HTMLResponse)
def blog_hotspot_detail(report_date: str, slug: str, request: Request, db: Session = Depends(get_db)):
    hotspot = find_hotspot_by_date_slug(db, report_date, slug)
    if hotspot is None:
        raise HTTPException(status_code=404, detail="hotspot item not found")
    item = exported_item_for_hotspot(hotspot, report_date=report_date)
    return templates.TemplateResponse("blog_item.html", {"request": request, "item": item})


@app.get("/daily-report", response_class=HTMLResponse)
def daily_report_page(request: Request, db: Session = Depends(get_db)):
    report = latest_daily_report(db)
    payload = build_export_payload(db)
    return templates.TemplateResponse(
        "daily_report.html",
        {
            "request": request,
            "report": report,
            "payload": payload,
            "running": is_running(),
        },
    )


@app.get("/raw-sources", response_class=HTMLResponse)
def raw_sources_page(request: Request, db: Session = Depends(get_db)):
    sources = [
        row[0]
        for row in db.execute(
            select(SourceItemModel.source).distinct().order_by(SourceItemModel.source)
        ).all()
    ]
    return templates.TemplateResponse(
        "raw_sources.html",
        {
            "request": request,
            "sources": sources,
        },
    )


async def _background_ingest() -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        await run_full_ingest(db)
        await export_blog_hotspots_with_llm(db)
        await ensure_daily_report(db)
    finally:
        db.close()


def _run_research_then_export(report_id: int, report_date: str | None = None) -> None:
    """后台运行研究任务，并在完成后刷新博客 JSON 缓存。"""

    asyncio.run(run_research_report(report_id))
    if not report_date:
        return

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        export_blog_hotspots(db, target_date=date.fromisoformat(report_date))
    except Exception:
        logging.exception("刷新博客研究缓存失败 report_id=%s report_date=%s", report_id, report_date)
    finally:
        db.close()


@app.api_route("/run-now", methods=["GET", "POST"])
async def run_now(background_tasks: BackgroundTasks):
    if is_running():
        return {"status": "already_running", "message": "采集任务正在运行，请稍后刷新。"}
    background_tasks.add_task(_background_ingest)
    return {"status": "started", "message": "已启动后台采集任务。"}


@app.get("/api/hotspots", response_model=list[HotspotOut])
def api_hotspots(status: str | None = None, db: Session = Depends(get_db)):
    return list_hotspots(db, status=status, hours=None if status else 48)


@app.post("/api/hotspots/{hotspot_id}/select", response_model=HotspotOut)
def api_select(hotspot_id: int, payload: SelectionIn | None = None, db: Session = Depends(get_db)):
    result = set_hotspot_status(db, hotspot_id, "select", payload.note if payload else None)
    if not result:
        raise HTTPException(status_code=404, detail="hotspot not found")
    return result


@app.post("/api/hotspots/{hotspot_id}/ignore", response_model=HotspotOut)
def api_ignore(hotspot_id: int, payload: SelectionIn | None = None, db: Session = Depends(get_db)):
    result = set_hotspot_status(db, hotspot_id, "ignore", payload.note if payload else None)
    if not result:
        raise HTTPException(status_code=404, detail="hotspot not found")
    return result


@app.post("/api/hotspots/{hotspot_id}/watch", response_model=HotspotOut)
def api_watch(hotspot_id: int, payload: SelectionIn | None = None, db: Session = Depends(get_db)):
    result = set_hotspot_status(db, hotspot_id, "watch", payload.note if payload else None)
    if not result:
        raise HTTPException(status_code=404, detail="hotspot not found")
    return result


@app.post("/api/hotspots/{hotspot_id}/research")
async def api_start_research(hotspot_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    existing = latest_research_report(db, hotspot_id)
    if existing is not None and existing.status == "running":
        return {"status": "already_running", "report_id": existing.id, "url": f"/research/{existing.id}"}
    report = create_research_report(db, hotspot_id)
    if report is None:
        raise HTTPException(status_code=404, detail="hotspot not found")
    threading.Thread(target=_run_research_then_export, args=(report.id,), daemon=True).start()
    return {"status": "started", "report_id": report.id, "url": f"/research/{report.id}"}


@app.get("/api/hotspots/{hotspot_id}/research/latest")
def api_latest_research(hotspot_id: int, db: Session = Depends(get_db)):
    report = latest_research_report(db, hotspot_id)
    if report is None:
        raise HTTPException(status_code=404, detail="research report not found")
    return {
        "id": report.id,
        "hotspot_id": report.hotspot_id,
        "status": report.status,
        "model": report.model,
        "report_markdown": report.report_markdown,
        "error_message": report.error_message,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "url": f"/research/{report.id}",
    }


@app.post("/api/source-items/{source_item_id}/github-info-expansion")
async def api_github_info_expansion(source_item_id: int, db: Session = Depends(get_db)):
    """手动启动单个 GitHub source item 的 AI 信息扩展。

    语义：
    - 已成功生成并写入 SQLite：拒绝重复生成。
    - 正在生成：返回 already_running。
    - 未开始/失败/达到自动重试上限：启动一轮后台任务；后台内部最多自动重试 3 次。
    """

    item = db.get(SourceItemModel, source_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"source item not found: {source_item_id}")
    if item.source not in {"github_trending_daily", "github"}:
        raise HTTPException(status_code=400, detail=f"source item is not GitHub source: {item.source}")
    if _github_item_has_expansion(db, source_item_id):
        raise HTTPException(status_code=409, detail="AI 检索结果已经成功生成并写入 SQLite，不能重复生成。")
    with _github_info_lock:
        if source_item_id in _github_info_running_ids:
            return {
                "status": "already_running",
                "source_item_id": source_item_id,
                "message": "AI 检索正在运行中，请稍后刷新。",
            }
        _github_info_running_ids.add(source_item_id)
        _github_info_failed.pop(source_item_id, None)
        _github_info_attempts[source_item_id] = 0

    _save_github_info_attempt_meta(
        db,
        source_item_id,
        {
            "status": "queued",
            "attempt": 0,
            "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
            "queued_at": _utc_now_iso(),
            "manual": True,
        },
    )
    threading.Thread(target=_run_github_info_expansion_worker, args=(source_item_id,), daemon=True).start()
    return {
        "status": "started",
        "source_item_id": source_item_id,
        "message": "已启动 AI 检索；失败会自动重试，最多 3 次。",
        "max_attempts": _GITHUB_INFO_MAX_ATTEMPTS,
    }


@app.post("/api/source-items/{source_item_id}/community-signals")
async def api_source_item_community_signals(source_item_id: int, db: Session = Depends(get_db)):
    """只运行社区/传播信号爬虫，便于前端调试每个平台的 raw/usable/discarded 输出。"""

    item = db.get(SourceItemModel, source_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"source item not found: {source_item_id}")
    if item.source not in {"github_trending_daily", "github"}:
        raise HTTPException(status_code=400, detail=f"source item is not GitHub source: {item.source}")

    result = await expand_and_save_community_signals(db, source_item_id)
    return {
        "status": "success",
        "source_item_id": source_item_id,
        "community_signals_context": result,
    }


@app.get("/api/source-items/{source_item_id}/community-signals")
def api_get_source_item_community_signals(source_item_id: int, db: Session = Depends(get_db)):
    """读取已保存的社区/传播信号爬虫结果。"""

    item = db.get(SourceItemModel, source_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"source item not found: {source_item_id}")
    raw_payload = _safe_load_json_object(item.raw_payload_json)
    result = raw_payload.get("github_community_signals")
    if not isinstance(result, dict):
        result = None
    return {
        "status": "success" if result else "empty",
        "source_item_id": source_item_id,
        "community_signals_context": result,
    }


@app.get("/api/integrations/reddit-devvit/watchlist")
def api_reddit_devvit_watchlist(request: Request, db: Session = Depends(get_db)):
    """给 Reddit Devvit app 拉取当前 GitHub 项目 watchlist。

    Devvit app 不使用传统 Reddit API key；它在 Reddit 平台内读取帖子，再按这里返回的
    owner/repo、repo URL、别名做本地匹配。
    """

    _require_reddit_devvit_token(request)
    settings = get_settings()
    limit = max(1, min(int(settings.reddit_devvit_watchlist_limit or 50), 200))
    rows = db.execute(
        select(SourceItemModel)
        .where(SourceItemModel.source.in_({"github_trending_daily", "github"}))
        .order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at))
        .limit(limit)
    ).scalars().all()

    items = []
    for item in rows:
        metrics = _safe_load_json_object(item.metrics_json)
        raw_payload = _safe_load_json_object(item.raw_payload_json)
        items.append(
            {
                "source_item_id": item.id,
                "project_key": item.source_item_id or item.title,
                "title": item.title,
                "repo_url": item.url,
                "aliases": _repo_aliases_for_watchlist(item, metrics, raw_payload),
                "rank": metrics.get("rank"),
                "stars_today": metrics.get("stars_today"),
            }
        )
    return {
        "status": "success",
        "version": "reddit_devvit_watchlist_v1",
        "generated_at": _utc_now_iso(),
        "count": len(items),
        "items": items,
    }


@app.post("/api/integrations/reddit-devvit/signals")
async def api_reddit_devvit_ingest_signals(request: Request, db: Session = Depends(get_db)):
    """接收 Reddit Devvit app 回传的社区信号，并写入对应 GitHub item。"""

    _require_reddit_devvit_token(request)
    payload = await request.json()
    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list):
        raise HTTPException(status_code=400, detail="signals must be a list")

    grouped: dict[int, list[dict]] = {}
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, dict):
            continue
        source_item_id = raw_signal.get("source_item_id") or raw_signal.get("sourceItemId") or payload.get("source_item_id")
        try:
            source_item_id_int = int(source_item_id)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(source_item_id_int, []).append(_normalize_devvit_signal(raw_signal, source_item_id_int))

    updated: list[dict] = []
    for source_item_id, signals in grouped.items():
        item = db.get(SourceItemModel, source_item_id)
        if item is None:
            continue
        raw_payload = _safe_load_json_object(item.raw_payload_json)

        stored = raw_payload.get("github_reddit_devvit_signals")
        if not isinstance(stored, dict):
            stored = {
                "version": "reddit_devvit_bridge_v1",
                "signals": [],
                "batches": [],
            }
        existing_signals = stored.get("signals") if isinstance(stored.get("signals"), list) else []
        by_url: dict[str, dict] = {
            str(signal.get("url") or f"idx:{idx}"): signal
            for idx, signal in enumerate(existing_signals)
            if isinstance(signal, dict)
        }
        for signal in signals:
            key = str(signal.get("url") or f"devvit:{signal.get('title')}:{signal.get('published_at')}")
            by_url[key] = signal
        merged_signals = list(by_url.values())[-100:]
        stored["signals"] = merged_signals
        stored["updated_at"] = _utc_now_iso()
        batches = stored.get("batches") if isinstance(stored.get("batches"), list) else []
        batches.append(
            {
                "received_at": _utc_now_iso(),
                "signal_count": len(signals),
                "source": payload.get("source") or "reddit_devvit",
            }
        )
        stored["batches"] = batches[-20:]
        raw_payload["github_reddit_devvit_signals"] = stored

        community = raw_payload.get("github_community_signals")
        if isinstance(community, dict):
            platforms = community.setdefault("platforms", {})
            platforms["reddit_devvit"] = {
                "summary": {
                    "status": "success" if merged_signals else "empty",
                    "signal_count": len(merged_signals),
                    "usable_signal_count": len(merged_signals),
                    "error": None,
                    "raw_signal_count": len(merged_signals),
                    "discarded_signal_count": 0,
                },
                "signals": merged_signals,
                "raw_signals": merged_signals,
                "discarded_signals": [],
            }
            existing_community_signals = community.get("signals") if isinstance(community.get("signals"), list) else []
            community["signals"] = [*existing_community_signals, *merged_signals][-200:]
            community["expanded_at"] = _utc_now_iso()
            raw_payload["github_community_signals"] = community

        item.raw_payload_json = json.dumps(raw_payload, ensure_ascii=False, default=str)
        db.add(item)
        updated.append({"source_item_id": source_item_id, "signal_count": len(signals)})

    db.commit()
    return {
        "status": "success",
        "version": "reddit_devvit_ingest_v1",
        "received_at": _utc_now_iso(),
        "updated": updated,
    }


@app.post("/api/blog-export/today")
async def api_export_blog_today(db: Session = Depends(get_db)):
    """生成博客可消费的 latest/archive/items JSON。

    该接口面向站点构建、定时任务或人工触发；不会调用深度研究模型。
    """

    return await export_blog_hotspots_with_llm(db)


@app.get("/api/blog-export/latest")
def api_blog_export_latest(db: Session = Depends(get_db)):
    """直接返回最新一期博客热点索引，便于博客后端/API Route 代理消费。"""

    payload = build_export_payload(db)
    write_blog_export_payload(payload)
    return {
        "status": "success",
        "date": payload["date"],
        "timezone": payload["timezone"],
        "generated_at": payload["generated_at"],
        "cards": payload["cards"],
    }


@app.post("/api/raw-sources/query")
async def api_raw_sources_query(request: Request, db: Session = Depends(get_db)):
    """查询原始 API payload。

    这是给前端调试/观察用的接口，返回 Adapter 入库时保存的 raw_payload_json，
    尽量展示 API 返回原貌，而不是 LLM 加工后的热点或文章。
    """

    payload = await request.json()
    source = (payload.get("source") or "").strip()
    keyword = (payload.get("keyword") or "").strip()
    limit = int(payload.get("limit") or 20)
    offset = int(payload.get("offset") or 0)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    filters = []
    if source:
        filters.append(SourceItemModel.source == source)
    if keyword:
        like = f"%{keyword}%"
        filters.append(
            or_(
                SourceItemModel.title.like(like),
                SourceItemModel.summary.like(like),
                SourceItemModel.raw_text.like(like),
                SourceItemModel.metrics_json.like(like),
                SourceItemModel.raw_payload_json.like(like),
            )
        )

    total = db.execute(select(func.count()).select_from(SourceItemModel).where(*filters)).scalar_one()
    stmt = select(SourceItemModel).where(*filters).order_by(desc(SourceItemModel.created_at))
    rows = db.execute(stmt.offset(offset).limit(limit)).scalars().all()
    items = []
    for item in rows:
        raw_payload_json = item.raw_payload_json or "{}"
        metrics_json = item.metrics_json or "{}"
        try:
            raw_payload = json.loads(raw_payload_json)
        except json.JSONDecodeError:
            raw_payload = raw_payload_json
        try:
            metrics = json.loads(metrics_json)
        except json.JSONDecodeError:
            metrics = {}
        items.append(
            {
                "id": item.id,
                "source": item.source,
                "source_item_id": item.source_item_id,
                "title": item.title,
                "url": item.url,
                "author": item.author,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "summary": item.summary,
                "raw_text": item.raw_text,
                "metrics": metrics,
                "metrics_json": metrics_json,
                "raw_payload": raw_payload,
                "raw_payload_json": raw_payload_json,
            }
        )
    return {
        "status": "success",
        "source": source or None,
        "keyword": keyword or None,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(items) < total,
        "count": len(items),
        "items": items,
    }


@app.post("/api/daily-report/today")
async def api_daily_report_today(force: bool = False, db: Session = Depends(get_db)):
    """生成 AI/科技今日报告。

    流程不是硬编码模板：
    1. 读取 API/RSS 采集后的热点与来源材料；
    2. Codex 先解析新闻并生成选题 brief；
    3. research agent 根据 brief 联网补充资料；
    4. Codex 写成完整 Markdown 日报并缓存。
    """

    existing = latest_daily_report(db)
    if force or existing is None or existing.status != "success":
        if is_running():
            return {
                "status": "already_running",
                "message": "新闻/API 采集任务正在运行，请稍后再生成日报。",
                "url": "/daily-report",
            }
        await run_full_ingest(db)
        await export_blog_hotspots_with_llm(db)

    report = await ensure_daily_report(db, force=force)
    if report is None:
        raise HTTPException(status_code=404, detail="暂无可用于生成日报的热点材料，请先执行采集。")
    return {
        "id": report.id,
        "report_date": report.report_date,
        "status": report.status,
        "model": report.model,
        "error_message": report.error_message,
        "generated_at": report.finished_at,
        "url": "/daily-report",
    }


@app.get("/api/daily-report/today")
def api_daily_report_latest(db: Session = Depends(get_db)):
    report = latest_daily_report(db)
    if report is None:
        raise HTTPException(status_code=404, detail="daily report not found")
    return {
        "id": report.id,
        "report_date": report.report_date,
        "status": report.status,
        "model": report.model,
        "report_markdown": report.report_markdown,
        "error_message": report.error_message,
        "generated_at": report.finished_at,
    }


@app.post("/api/blog-export/today/llm")
async def api_export_blog_today_llm(force: bool = False, db: Session = Depends(get_db)):
    return await export_blog_hotspots_with_llm(db, force_analysis=force)


@app.get("/api/blog-export/items/{report_date}/{slug}")
def api_blog_export_item(report_date: str, slug: str, db: Session = Depends(get_db)):
    hotspot = find_hotspot_by_date_slug(db, report_date, slug)
    if hotspot is None:
        raise HTTPException(status_code=404, detail="hotspot item not found")
    return exported_item_for_hotspot(hotspot, report_date=report_date)


@app.post("/api/blog-export/items/{report_date}/{slug}/deep-research")
async def api_blog_item_deep_research(report_date: str, slug: str, db: Session = Depends(get_db)):
    """触发 C 层深度研究：成功结果永久复用，不提供刷新语义。

    - 已有 success：直接返回缓存；
    - 正在 running：返回 running；
    - 无 success/running：创建一次 blog_style 研究任务。
    """

    hotspot = find_hotspot_by_date_slug(db, report_date, slug)
    if hotspot is None:
        raise HTTPException(status_code=404, detail="hotspot item not found")

    success = latest_success_research_report(db, hotspot.id)
    if success is not None:
        return {
            "status": "cached",
            "report_id": success.id,
            "hotspot_id": hotspot.id,
            "report_markdown": success.report_markdown,
            "generated_at": success.finished_at,
        }

    existing = latest_research_report(db, hotspot.id)
    if existing is not None and existing.status == "running":
        return {"status": "already_running", "report_id": existing.id, "hotspot_id": hotspot.id}

    report = create_research_report(db, hotspot.id, blog_style=True)
    if report is None:
        raise HTTPException(status_code=404, detail="hotspot not found")
    threading.Thread(target=_run_research_then_export, args=(report.id, report_date), daemon=True).start()
    return {"status": "started", "report_id": report.id, "hotspot_id": hotspot.id}
