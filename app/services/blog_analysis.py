from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.model_utils import first_model_candidate, format_model_candidates, parse_model_candidates
from app.models import BlogAnalysisModel, HotspotModel, HotspotSourceModel

logger = logging.getLogger(__name__)

ANALYSIS_VERSION = "blog_article_v2"


def _analysis_base_url() -> str:
    settings = get_settings()
    base_url = settings.blog_analysis_base_url or settings.openai_base_url
    if not base_url:
        raise ValueError("未配置 OPENAI_BASE_URL 或 BLOG_ANALYSIS_BASE_URL，无法调用 Codex 写作接口")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _analysis_model() -> str:
    settings = get_settings()
    return first_model_candidate(settings.blog_analysis_model or settings.openai_model, default="")


def _analysis_model_candidates() -> list[str]:
    settings = get_settings()
    return parse_model_candidates(settings.blog_analysis_model or settings.openai_model)


def _analysis_model_label() -> str:
    label = format_model_candidates(_analysis_model_candidates())
    return label or _analysis_model()


def _analysis_headers() -> dict[str, str]:
    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _json_loads(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def _extract_content(response: httpx.Response) -> str:
    text = response.text.strip()
    try:
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        pass

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
    return "".join(chunks) if chunks else text


async def _request_analysis_completion(messages: list[dict[str, str]]) -> tuple[str, str]:
    settings = get_settings()
    candidates = _analysis_model_candidates() or [_analysis_model()]
    last_error: Exception | None = None
    max_attempts = 5

    async with httpx.AsyncClient(timeout=settings.blog_analysis_timeout_seconds) as client:
        for index, model_name in enumerate(candidates):
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.2,
            }
            for attempt in range(max_attempts):
                try:
                    response = await client.post(_analysis_base_url(), json=payload, headers=_analysis_headers())
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
                logger.warning("博客写作模型 %s 调用失败，回退到 %s：%s", model_name, candidates[index + 1], last_error)

    if last_error is not None:
        raise last_error
    raise RuntimeError("博客写作接口调用失败")


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.S | re.I).strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned.strip(), flags=re.I).strip()
    cleaned = re.sub(r"```$", "", cleaned.strip()).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError("Codex 写作接口未返回可解析的 JSON 对象")


def hotspot_payload(hotspot: HotspotModel) -> dict[str, Any]:
    sources = []
    for link in hotspot.sources:
        item = link.source_item
        sources.append(
            {
                "source": item.source,
                "title": item.title,
                "url": item.url,
                "author": item.author,
                "summary": item.summary,
                "raw_text": item.raw_text,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "metrics": _json_loads(item.metrics_json, {}),
            }
        )
    return {
        "hotspot_id": hotspot.id,
        "title": hotspot.title,
        "summary": hotspot.summary,
        "why_it_matters": hotspot.why_it_matters,
        "content_type": hotspot.content_type,
        "score": hotspot.score,
        "risk": hotspot.risk,
        "sources": sources,
    }


def build_blog_analysis_prompt(hotspot: HotspotModel) -> list[dict[str, str]]:
    payload = hotspot_payload(hotspot)
    system = (
        "你是一个客观克制的 AI/科技自媒体编辑。"
        "你要为个人博客的单个热点写一篇短文章，风格像科技自媒体，但保持客观、克制、可信。"
        "只基于输入材料和你能确认的公开常识进行写作，不编造事实。"
        "输出必须是严格 JSON，不要 Markdown，不要代码块，不要第一人称。"
    )
    user = f"""
请为下面热点生成博客展示用结构化内容，但重点是写成一篇可直接发布的短文章。

热点 JSON：
{json.dumps(payload, ensure_ascii=False, indent=2)}

输出严格 JSON，字段必须完整：

{{
  "card": {{
    "title": "适合博客卡片展示的标题，尽量保留项目/产品名",
    "summary": "一句话摘要，50-90 个中文字符，说明它是什么、发布了什么、有哪些公开信号",
    "importance_reason": "一句话说明为什么它会进入热点视野，尽量用公开信号解释",
    "type_label": "AI 工具测评/开源项目/论文解读/产品发现/科技新闻之一"
  }},
  "article": {{
    "title": "适合文章页展示的标题，可以更像科技自媒体",
    "content": "一篇 400-800 字的中文短文，分 3-5 个自然段，不要用要点列表，不要使用小标题。开头要抓住重点，中间讲清楚它是什么、有哪些关键事实、为什么会成为热点、它和哪些公开来源有关，结尾做一个克制收束。每段之间用空行分隔。"
  }}
}}

要求：
- 不要出现“小红书”“爆款”“我认为”等表达。
- 不要把未验证的信息写成确定事实。
- 不要写成报告分节，不要用要点列表。
- 不要输出 JSON 以外的任何文字。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def latest_blog_analysis(db: Session, hotspot_id: int) -> BlogAnalysisModel | None:
    return db.execute(
        select(BlogAnalysisModel)
        .where(
            BlogAnalysisModel.hotspot_id == hotspot_id,
            BlogAnalysisModel.analysis_version == ANALYSIS_VERSION,
        )
        .order_by(desc(BlogAnalysisModel.created_at))
        .limit(1)
    ).scalar_one_or_none()


def analysis_data_for_hotspot(hotspot: HotspotModel) -> dict[str, Any] | None:
    analyses = [a for a in hotspot.blog_analyses if a.analysis_version == ANALYSIS_VERSION and a.status == "success"]
    if not analyses:
        return None
    latest = sorted(analyses, key=lambda a: a.created_at, reverse=True)[0]
    data = _json_loads(latest.analysis_json, None)
    return data if isinstance(data, dict) else None


async def ensure_blog_analysis(db: Session, hotspot_id: int, *, force: bool = False) -> BlogAnalysisModel | None:
    settings = get_settings()
    if not settings.enable_blog_analysis_llm:
        return latest_blog_analysis(db, hotspot_id)

    existing = latest_blog_analysis(db, hotspot_id)
    if existing is not None and existing.status == "success" and not force:
        return existing
    if existing is not None and existing.status == "running" and not force:
        return existing

    hotspot = db.execute(
        select(HotspotModel)
        .where(HotspotModel.id == hotspot_id)
        .options(selectinload(HotspotModel.sources).selectinload(HotspotSourceModel.source_item))
    ).scalar_one_or_none()
    if hotspot is None:
        return None

    messages = build_blog_analysis_prompt(hotspot)
    if existing is None:
        analysis = BlogAnalysisModel(
            hotspot_id=hotspot_id,
            analysis_version=ANALYSIS_VERSION,
            status="running",
            model=_analysis_model_label(),
            prompt_json=json.dumps(messages, ensure_ascii=False),
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
    else:
        analysis = existing
        analysis.status = "running"
        analysis.model = _analysis_model_label()
        analysis.prompt_json = json.dumps(messages, ensure_ascii=False)
        analysis.error_message = None
        analysis.started_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(analysis)

    try:
        content, used_model = await _request_analysis_completion(messages)
        data = _parse_json_object(content)
        analysis.analysis_json = json.dumps(data, ensure_ascii=False)
        analysis.raw_response = content[:12000]
        analysis.model = used_model
        analysis.status = "success"
        analysis.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(analysis)
        return analysis
    except Exception as exc:
        logger.exception("博客 A/B 层 Codex 写作失败 hotspot_id=%s", hotspot_id)
        analysis.status = "failed"
        if isinstance(exc, httpx.HTTPStatusError):
            analysis.error_message = f"{exc}; response={exc.response.text[:2000]}"[:4000]
        else:
            analysis.error_message = str(exc)[:4000]
        analysis.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(analysis)
        return analysis


async def ensure_blog_analyses_for_hotspots(db: Session, hotspots: list[HotspotModel], *, force: bool = False) -> None:
    for hotspot in hotspots:
        await ensure_blog_analysis(db, hotspot.id, force=force)
