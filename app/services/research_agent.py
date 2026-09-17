from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.model_utils import first_model_candidate, format_model_candidates, parse_model_candidates
from app.models import HotspotModel, HotspotSourceModel, ResearchReportModel

logger = logging.getLogger(__name__)


def _codex_chat_url() -> str:
    """返回 Codex/OpenAI 兼容 chat completions 地址。

    C 层深度研究里，联网资料搜集由 research agent 完成；最终报告撰写由
    OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL 指向的 Codex 兼容接口完成。
    """

    settings = get_settings()
    base_url = settings.openai_base_url
    if not base_url:
        raise ValueError("未配置 OPENAI_BASE_URL，无法调用 Codex 写作接口")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _codex_headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ValueError("未配置 OPENAI_API_KEY，无法调用 Codex 写作接口")
    return {"Authorization": f"Bearer {settings.openai_api_key}"}


def _codex_model() -> str:
    settings = get_settings()
    return first_model_candidate(settings.openai_model, default="")


def _codex_model_candidates() -> list[str]:
    settings = get_settings()
    return parse_model_candidates(settings.openai_model)


def _codex_model_label() -> str:
    label = format_model_candidates(_codex_model_candidates())
    return label or _codex_model()


async def _request_codex_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    timeout_seconds: float | None = None,
) -> tuple[str, str]:
    settings = get_settings()
    candidates = _codex_model_candidates() or [_codex_model()]
    last_error: Exception | None = None
    max_attempts = 5

    async with httpx.AsyncClient(timeout=timeout_seconds or settings.research_agent_timeout_seconds) as client:
        for index, model_name in enumerate(candidates):
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
            }
            for attempt in range(max_attempts):
                try:
                    response = await client.post(_codex_chat_url(), json=payload, headers=_codex_headers())
                    response.raise_for_status()
                    return _extract_report_content(response), model_name
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
                logger.warning("Codex 写作模型 %s 调用失败，回退到 %s：%s", model_name, candidates[index + 1], last_error)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Codex 写作接口调用失败")


def _extract_report_content(response: httpx.Response) -> str:
    text = response.text.strip()
    try:
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        pass

    # 兼容 SSE：data: {...}
    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
            delta = data.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content") or data.get("choices", [{}])[0].get("message", {}).get("content")
            if content:
                chunks.append(content)
        except json.JSONDecodeError:
            continue
    if chunks:
        return "".join(chunks)

    # 部分本地代理会返回带 <think> 的纯文本；直接作为报告保存。
    if text:
        return re.sub(r"^\s*data:\s*", "", text)
    raise ValueError("research agent returned empty response")


def _hotspot_payload(hotspot: HotspotModel) -> dict[str, Any]:
    source_items = []
    for link in hotspot.sources:
        item = link.source_item
        try:
            metrics = json.loads(item.metrics_json or "{}")
        except json.JSONDecodeError:
            metrics = {}
        source_items.append({
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "author": item.author,
            "summary": item.summary,
            "raw_text": item.raw_text,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "metrics": metrics,
        })
    return {
        "hotspot_id": hotspot.id,
        "title": hotspot.title,
        "summary": hotspot.summary,
        "why_it_matters": hotspot.why_it_matters,
        "xiaohongshu_angle": hotspot.xiaohongshu_angle,
        "content_type": hotspot.content_type,
        "score": hotspot.score,
        "risk": hotspot.risk,
        "sources": source_items,
    }


def build_research_prompt(hotspot: HotspotModel, *, blog_style: bool = False) -> list[dict[str, str]]:
    payload = _hotspot_payload(hotspot)
    if blog_style:
        system = (
            "你是 AI/科技资料搜集子 Agent。你的任务不是写最终文章，而是围绕一个 AI/科技热点，"
            "利用可用联网搜索能力尽可能广泛地搜集公开资料、证据链接、时间线、争议和待验证点。"
            "不要编造来源；如果无法验证，请明确标注“待验证”。输出必须是中文 Markdown 资料包。"
        )
        user = f"""
请围绕下面热点生成一份“资料搜集包”，后续会交给 Codex 写成最终博客深度报告。

热点 JSON：
{json.dumps(payload, ensure_ascii=False, indent=2)}

必须遵守：

1. 重点是搜集事实、链接和证据，不要写成最终文章。
2. 尽可能使用联网搜索补充资料，但不能编造来源。
3. 必须包含明确来源链接，并说明来源支持了什么信息点。
4. 如果资料互相冲突，要说明冲突和局限。
5. 不出现“我认为”“我的判断”等第一人称表达。

请输出 Markdown，结构如下：

## 资料包摘要
用 5-10 条 bullet 概括当前已搜集到的可信信息和待验证信息。

## 关键事实与证据
列出可以被来源支持的事实，每条包含来源 URL。

## 时间线
按时间顺序列出关键事件；不确定项标注“待验证”。

## 背景资料
解释相关公司、项目、论文、工具、人物、技术路线或市场背景。

## 技术/产品观察
整理它解决什么问题、与同类方向的关系、实际可用性和限制；只写可由资料支持的观察。

## 争议与风险
列出事实风险、夸大风险、版权/许可/伦理/安全风险。

## 来源与证据表格
用表格列出：
| 来源标题 | URL | 支持的信息点 | 可信度/局限 |

## 资料缺口
列出当前公开材料中仍缺失、冲突或无法确认的信息。
"""
    else:
        system = (
            "你是 AI/科技内容选题研究子 Agent。你的任务是围绕用户选中的热点，"
            "尽可能系统地整理相关背景、事实、关联项目、争议点和资料缺口。"
            "你不能编造不存在的事实；如果需要外部检索能力，请基于你可用的工具/内置检索进行。"
            "如果无法验证，请明确标注“待验证”。输出必须是中文 Markdown。"
        )
        user = f"""
请围绕下面热点做深度资料搜集和选题研究。

热点 JSON：
{json.dumps(payload, ensure_ascii=False, indent=2)}

请输出 Markdown，包含以下部分：

## 摘要
用 3-5 条 bullet 概括当前可以确认的信息。

## 事实时间线
按时间列出已知关键事件；不确定信息标注“待验证”。

## 背景资料
解释相关公司/项目/论文/工具/人物/技术背景。

## 多源信息与链接
列出你能找到或从输入中确认的关键来源链接，并说明每个链接提供了什么信息。

## 争议与风险
列出事实风险、夸大风险、法律/版权/伦理/平台表达风险。

## 资料缺口
列出当前公开材料中仍缺失、冲突或无法确认的信息。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_codex_report_prompt(
    hotspot_payload: dict[str, Any],
    research_pack_markdown: str,
) -> list[dict[str, str]]:
    """把 research agent 资料包交给 Codex，生成 C 层最终博客报告。"""

    system = (
        "你是一个客观克制的 AI/科技媒体主笔。"
        "你将收到原始热点 JSON 和一个由联网研究子 Agent 生成的资料搜集包。"
        "你的任务是基于这些材料写成个人博客可发布的中文深度研究报告。"
        "不要编造资料包中没有的事实；资料包没有证据支持的信息必须标注“待验证”。"
        "不要写成小红书文案，不要使用第一人称，不要输出写作过程。"
    )
    user = f"""
请基于下面材料生成一份 3000-5000 字中文 Markdown 深度研究报告。

原始热点 JSON：
{json.dumps(hotspot_payload, ensure_ascii=False, indent=2)}

联网资料搜集包：
{research_pack_markdown}

必须遵守：

1. 最终报告使用科技媒体风，客观、中性、克制。
2. 只基于输入材料写作；不确定或冲突的信息明确标注“待验证”或“存在信息冲突”。
3. 所有关键事实尽量附来源链接；不要伪造链接。
4. 不出现“我认为”“我的判断”“小红书”“爆款”等表达。
5. 输出 Markdown，不要输出 JSON，不要输出代码块。

请使用以下结构：

## 摘要
用 3-5 段概括事件、背景和当前可信结论。

## 关键事实
列出 5-8 条可以被来源支持的事实。

## 事件背景
解释相关公司、项目、论文、工具、人物、技术路线或市场背景。

## 时间线
按时间顺序列出关键事件；不确定项标注“待验证”。

## 技术/产品背景
说明它的技术路线、产品形态、相关项目、公开指标和已知限制。

## 争议与风险
列出事实风险、夸大风险、版权/许可/伦理/安全/平台表达风险。

## 来源与证据表
用表格列出：
| 来源标题 | URL | 支持的信息点 | 可信度/局限 |

## 资料缺口
列出当前公开材料中仍缺失、冲突或无法确认的信息。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def create_research_report(db: Session, hotspot_id: int, *, blog_style: bool = False) -> ResearchReportModel | None:
    settings = get_settings()
    hotspot = db.execute(
        select(HotspotModel)
        .where(HotspotModel.id == hotspot_id)
        .options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
    ).scalar_one_or_none()
    if hotspot is None:
        return None
    messages = build_research_prompt(hotspot, blog_style=blog_style)
    report = ResearchReportModel(
        hotspot_id=hotspot_id,
        status="running",
        model=settings.research_agent_model,
        prompt_json=json.dumps(messages, ensure_ascii=False),
        sources_json=json.dumps(_hotspot_payload(hotspot)["sources"], ensure_ascii=False, default=str),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


async def run_research_report(report_id: int) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        report = db.get(ResearchReportModel, report_id)
        if report is None:
            return
        messages = json.loads(report.prompt_json)
        prompt_text = json.dumps(messages, ensure_ascii=False)
        is_blog_style = "资料搜集包" in prompt_text or "最终博客深度报告" in prompt_text
        payload = {
            "model": settings.research_agent_model,
            "messages": messages,
            "temperature": 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=settings.research_agent_timeout_seconds) as client:
                response = await client.post(settings.research_agent_base_url, json=payload)
                response.raise_for_status()
            research_pack = _extract_report_content(response)

            if is_blog_style:
                hotspot = db.execute(
                    select(HotspotModel)
                    .where(HotspotModel.id == report.hotspot_id)
                    .options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
                ).scalar_one_or_none()
                hotspot_payload = _hotspot_payload(hotspot) if hotspot is not None else {"hotspot_id": report.hotspot_id}
                codex_messages = build_codex_report_prompt(hotspot_payload, research_pack)
                final_report, used_model = await _request_codex_completion(
                    codex_messages,
                    temperature=0.2,
                    timeout_seconds=settings.research_agent_timeout_seconds,
                )
                report.report_markdown = final_report
                report.model = f"{settings.research_agent_model} -> {used_model}"
            else:
                report.report_markdown = research_pack
            report.status = "success"
            report.finished_at = datetime.now(timezone.utc)
        except Exception as exc:
            logger.exception("研究子 Agent 运行失败 report_id=%s", report_id)
            report.status = "failed"
            if isinstance(exc, httpx.HTTPStatusError):
                report.error_message = f"{exc}; response={exc.response.text[:2000]}"[:4000]
            else:
                report.error_message = str(exc)[:4000]
            report.finished_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def latest_research_report(db: Session, hotspot_id: int) -> ResearchReportModel | None:
    return db.execute(
        select(ResearchReportModel)
        .where(ResearchReportModel.hotspot_id == hotspot_id)
        .order_by(desc(ResearchReportModel.created_at))
        .limit(1)
    ).scalar_one_or_none()


def latest_success_research_report(db: Session, hotspot_id: int) -> ResearchReportModel | None:
    return db.execute(
        select(ResearchReportModel)
        .where(and_(ResearchReportModel.hotspot_id == hotspot_id, ResearchReportModel.status == "success"))
        .order_by(desc(ResearchReportModel.created_at))
        .limit(1)
    ).scalar_one_or_none()


def get_research_report(db: Session, report_id: int) -> ResearchReportModel | None:
    return db.execute(
        select(ResearchReportModel)
        .where(ResearchReportModel.id == report_id)
        .options(selectinload(ResearchReportModel.hotspot).selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
    ).scalar_one_or_none()
