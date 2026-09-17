from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.model_utils import first_model_candidate, format_model_candidates, parse_model_candidates
from app.models import DailyReportModel, HotspotModel, HotspotSourceModel
from app.services.blog_analysis import _analysis_headers, _extract_content, _parse_json_object
from app.services.research_agent import _codex_chat_url, _hotspot_payload

logger = logging.getLogger(__name__)
DAILY_REPORT_VERSION = "ai_tech_daily_v1"


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(get_settings().timezone)


def _now_local() -> datetime:
    return datetime.now(_tz())


def _daily_report_model() -> str:
    settings = get_settings()
    return first_model_candidate(settings.blog_analysis_model or settings.openai_model, default="")


def _daily_report_model_candidates() -> list[str]:
    settings = get_settings()
    return parse_model_candidates(settings.blog_analysis_model or settings.openai_model)


def _daily_report_model_label() -> str:
    label = format_model_candidates(_daily_report_model_candidates())
    return label or _daily_report_model()


async def _request_daily_writer(messages: list[dict[str, str]]) -> tuple[str, str]:
    settings = get_settings()
    candidates = _daily_report_model_candidates() or [_daily_report_model()]
    last_error: Exception | None = None
    max_attempts = 5

    async with httpx.AsyncClient(timeout=settings.daily_report_timeout_seconds) as client:
        for index, model_name in enumerate(candidates):
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.2,
            }
            for attempt in range(max_attempts):
                try:
                    response = await client.post(_codex_chat_url(), json=payload, headers=_analysis_headers())
                    response.raise_for_status()
                    return _extract_content(response), model_name
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                        await asyncio.sleep(min(20.0, 2.0 * (attempt + 1)))
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(min(20.0, 2.0 * (attempt + 1)))
                        continue
                    break

            if index < len(candidates) - 1:
                logger.warning("日报写作模型 %s 调用失败，回退到 %s：%s", model_name, candidates[index + 1], last_error)

    if last_error is not None:
        raise last_error
    raise RuntimeError("日报写作接口调用失败")


def _latest_report(db: Session, report_date: str) -> DailyReportModel | None:
    return db.execute(
        select(DailyReportModel)
        .where(
            DailyReportModel.report_date == report_date,
            DailyReportModel.report_type == DAILY_REPORT_VERSION,
        )
        .order_by(desc(DailyReportModel.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _query_hotspots(db: Session, *, target_date: date | None = None, limit: int | None = None) -> list[HotspotModel]:
    settings = get_settings()
    limit = limit or settings.daily_report_max_hotspots
    stmt = (
        select(HotspotModel)
        .options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
        .order_by(desc(HotspotModel.score), desc(HotspotModel.created_at))
    )
    if target_date is None:
        since_local = _now_local() - timedelta(hours=settings.daily_report_lookback_hours)
        stmt = stmt.where(HotspotModel.created_at >= since_local.astimezone(timezone.utc))
    else:
        start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=_tz())
        end_local = start_local + timedelta(days=1)
        stmt = stmt.where(
            HotspotModel.created_at >= start_local.astimezone(timezone.utc),
            HotspotModel.created_at < end_local.astimezone(timezone.utc),
        )
    return list(db.execute(stmt.limit(limit)).scalars().all())


def _source_snapshot(hotspots: list[HotspotModel]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for hotspot in hotspots:
        payload = _hotspot_payload(hotspot)
        snapshot.append(
            {
                "hotspot_id": payload["hotspot_id"],
                "title": payload["title"],
                "summary": payload["summary"],
                "content_type": payload["content_type"],
                "score": payload["score"],
                "risk": payload["risk"],
                "sources": payload["sources"],
            }
        )
    return snapshot


def _daily_brief_prompt(report_date: str, hotspots: list[HotspotModel]) -> list[dict[str, str]]:
    base_sources = _source_snapshot(hotspots)
    system = (
        "你是一个 AI/科技日报选题编辑。你会收到今日从免费 API/RSS/站点聚合而来的原始热点材料。"
        "你的任务是阅读这些材料，筛选真正值得写入今日报告的主题，并生成后续联网搜集资料的 brief。"
        "只基于输入材料做判断，不要编造事实。输出必须是严格 JSON。"
    )
    user = f"""
今天日期：{report_date}

今日热点原始材料 JSON：
{json.dumps(base_sources, ensure_ascii=False, indent=2)}

请输出严格 JSON，不要 Markdown，不要代码块，字段如下：

{{
  "main_theme": "今天这些热点背后的共同主线，80-160 字",
  "selected_hotspots": [
    {{
      "hotspot_id": 123,
      "title": "热点标题",
      "reason": "为什么值得进入日报",
      "source_urls": ["输入材料中已有的 URL"]
    }}
  ],
  "search_questions": [
    "后续联网搜集时需要回答的问题，8-15 个"
  ],
  "report_outline": [
    "最终报告建议结构，5-8 个章节"
  ],
  "risk_notes": [
    "哪些点必须标注待验证或谨慎表达"
  ]
}}

要求：
1. 只选择 5-10 个真正值得写的热点。
2. 优先选择 AI 工具、开源项目、论文/模型、产品更新、产业信号。
3. search_questions 要能指导联网搜索补充背景、竞品、发布时间、官方来源、争议和限制。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _daily_research_prompt(report_date: str, source_snapshot: list[dict[str, Any]], editor_brief: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "你是 AI/科技日报资料搜集 Agent。你会收到今日原始热点材料和选题编辑 brief。"
        "你的任务是根据 brief 中的 search_questions 使用联网能力补充事实、背景、来源链接和争议信息。"
        "不要写最终报告，不要编造来源；无法确认的信息必须标注“待验证”。输出中文 Markdown 资料包。"
    )
    user = f"""
今天日期：{report_date}

原始热点材料：
{json.dumps(source_snapshot, ensure_ascii=False, indent=2)}

选题编辑 brief：
{json.dumps(editor_brief, ensure_ascii=False, indent=2)}

请输出 Markdown 资料包，结构如下：

## 资料包摘要
用 bullet 概括今天最重要的事实和趋势。

## 按主题补充资料
围绕 brief 中 selected_hotspots 和 search_questions，逐项补充公开来源、背景和关键事实。

## 时间线
整理可确认的发布时间、项目更新、论文/模型发布、社区讨论节点。

## 争议、限制与待验证点
列出不能写死的内容、资料冲突、营销夸大风险、许可/版权/安全风险。

## 来源与证据表
用表格列出：来源标题、URL、支持的信息点、可信度/局限。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _daily_report_prompt(
    report_date: str,
    source_snapshot: list[dict[str, Any]],
    editor_brief: dict[str, Any],
    research_pack: str,
) -> list[dict[str, str]]:
    system = (
        "你是一个 AI/科技日报主笔。你会收到今日 API/RSS 聚合材料、选题编辑 brief、联网资料搜集包。"
        "请基于这些材料写成一篇个人博客可发布的中文 AI/科技今日报告。"
        "不要编造事实，不要把未验证内容写成确定事实，必须保留来源链接。"
    )
    user = f"""
今天日期：{report_date}

今日热点原始材料 JSON：
{json.dumps(source_snapshot, ensure_ascii=False, indent=2)}

选题编辑 brief：
{json.dumps(editor_brief, ensure_ascii=False, indent=2)}

联网研究资料包：
{research_pack}

请输出 Markdown，结构如下：

## 今日结论
先给出今天最主要的 3-5 个事实性观察。

## 热点速览
按条目列出今天值得写的热点，每条包含：
- 标题
- 一句话说明
- 为什么会成为热点
- 来源链接

## 背景串联
把多个热点之间的共同趋势串起来，解释今天 AI/科技圈在发生什么。

## 值得注意的细节
列出容易被忽略但重要的事实、门槛、限制、争议或潜在机会。

## 事实背景与热点原因
客观说明这些热点的公开信号、技术背景、社区讨论和已知限制。

## 风险与待验证
明确哪些信息需要后续确认，哪些表达不能夸大。

## 来源与证据
用表格列出来源标题、URL、支持的信息点、可信度/局限。

要求：
1. 保持客观、中性、克制。
2. 不要写成营销文案。
3. 不要使用第一人称。
4. 不要输出 JSON。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def ensure_daily_report(db: Session, *, target_date: date | None = None, force: bool = False) -> DailyReportModel | None:
    settings = get_settings()
    report_date = (target_date or _now_local().date()).isoformat()
    existing = _latest_report(db, report_date)
    if existing is not None and existing.status == "success" and not force:
        return existing
    if existing is not None and existing.status == "running" and not force:
        return existing

    hotspots = _query_hotspots(db, target_date=target_date, limit=settings.daily_report_max_hotspots)
    if not hotspots:
        return existing

    source_snapshot = _source_snapshot(hotspots)
    prompt_messages = _daily_brief_prompt(report_date, hotspots)
    if existing is None:
        report = DailyReportModel(
            report_date=report_date,
            report_type=DAILY_REPORT_VERSION,
            status="running",
            model=_daily_report_model_label(),
            prompt_json=json.dumps(prompt_messages, ensure_ascii=False),
            source_snapshot_json=json.dumps(source_snapshot, ensure_ascii=False),
        )
        db.add(report)
        db.commit()
        db.refresh(report)
    else:
        report = existing
        report.status = "running"
        report.model = _daily_report_model_label()
        report.prompt_json = json.dumps(prompt_messages, ensure_ascii=False)
        report.source_snapshot_json = json.dumps(source_snapshot, ensure_ascii=False)
        report.error_message = None
        report.started_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)

    try:
        # 第一步：Codex 先阅读全部 API/RSS 原始材料，生成今日选题 brief。
        brief_content, brief_model = await _request_daily_writer(prompt_messages)
        editor_brief = _parse_json_object(brief_content)

        # 第二步：研究 agent 根据 brief 联网扩展今日资料包。
        research_messages = _daily_research_prompt(report_date, source_snapshot, editor_brief)
        research_payload = {
            "model": get_settings().research_agent_model,
            "messages": research_messages,
            "temperature": 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=settings.research_agent_timeout_seconds) as client:
                research_response = await client.post(get_settings().research_agent_base_url, json=research_payload)
                research_response.raise_for_status()
            research_pack = _extract_content(research_response)
        except Exception as research_exc:
            logger.exception("日报联网资料搜集失败，将仅基于 API/RSS 原始材料生成 report_date=%s", report_date)
            research_pack = (
                "## 联网资料搜集状态\n"
                f"研究 Agent 调用失败，最终报告只能基于 API/RSS 原始材料和选题 brief 生成。错误：{str(research_exc)[:500]}\n\n"
                "## 写作限制\n"
                "无法联网补充验证的内容必须标注“待验证”，不要扩展输入材料之外的事实。"
            )

        # 第三步：Codex 基于原始材料、编辑 brief 和资料包写完整日报。
        codex_messages = _daily_report_prompt(report_date, source_snapshot, editor_brief, research_pack)
        final_report, report_model = await _request_daily_writer(codex_messages)
        report.report_markdown = final_report
        report.research_pack_markdown = research_pack
        report.prompt_json = json.dumps(
            {
                "brief_messages": prompt_messages,
                "editor_brief": editor_brief,
                "research_messages": research_messages,
                "report_messages": codex_messages,
            },
            ensure_ascii=False,
        )
        report.model = f"{brief_model} -> {get_settings().research_agent_model} -> {report_model}"
        report.status = "success"
        report.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)
        return report
    except Exception as exc:
        logger.exception("生成今日报告失败 report_date=%s", report_date)
        report.status = "failed"
        report.error_message = str(exc)[:4000]
        report.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(report)
        return report


def latest_daily_report(db: Session, report_date: str | None = None) -> DailyReportModel | None:
    report_date = report_date or _now_local().date().isoformat()
    return _latest_report(db, report_date)
