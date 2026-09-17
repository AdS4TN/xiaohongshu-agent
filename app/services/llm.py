from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.model_utils import parse_model_candidates
from app.models import SourceItemModel
from app.services.scoring import infer_content_type

logger = logging.getLogger(__name__)


def fallback_summary(item: SourceItemModel, score: float) -> dict[str, Any]:
    content_type = infer_content_type(item)
    try:
        metrics = json.loads(item.metrics_json or "{}")
    except json.JSONDecodeError:
        metrics = {}
    summary = (item.summary or item.raw_text or item.title or "")[:120]
    if not summary:
        summary = f"{item.title} 是来自 {item.source} 的 AI/科技信号。"
    why_by_source = {
        "github": f"这个 GitHub 项目当前约 {metrics.get('stars', 0)} stars，近期仍有更新或较高关注度，适合判断是否具备产品化/测评价值。",
        "github_trending_daily": (
            f"这个项目进入 GitHub Trending Daily，页面口径显示今日新增约 {metrics.get('stars_today', 0)} stars、"
            f"累计约 {metrics.get('stars', 0)} stars，适合优先判断它为什么今天突然被开发者注意到。"
        ),
        "hf_spaces": f"这个 HuggingFace Space 有可体验 Demo，当前约 {metrics.get('likes', 0)} likes，适合优先验证真实使用效果。",
        "hf_models": f"这个 HuggingFace 模型当前约 {metrics.get('likes', 0)} likes、{metrics.get('downloads', 0)} downloads，说明社区已有一定关注。",
        "hf_daily_papers": "这篇论文进入 HuggingFace Daily Papers，说明它已经经过社区初筛，适合转译成普通人能理解的趋势内容。",
        "arxiv": "这是一篇新近 arXiv AI 相关论文，适合观察底层技术趋势，但需要进一步判断是否能转化为大众选题。",
        "hackernews": f"这条内容在 Hacker News 有约 {metrics.get('score', 0)} 分、{metrics.get('descendants', 0)} 条讨论，反映海外技术圈正在关注。",
    }
    angle_by_type = {
        "github_project": "从“这个开源项目解决什么问题、普通人/创作者能不能用、是否有 Demo、和同类工具相比怎样”切入。",
        "ai_tool_review": "从“我实际试用这个 AI Demo 后，发现它适合谁、不适合谁、能不能替代现有工作流”切入。",
        "product_discovery": "从“这个模型/产品能力有什么新鲜点、普通用户能在哪些场景用上、是否值得收藏”切入。",
        "paper_explainer": "从“这篇论文到底讲了什么、它可能影响哪些 AI 工具、普通人需要关注什么”切入。",
        "tech_news": "从“这件事为什么在技术圈被讨论、它对 AI 工具/产品趋势有什么启发”切入。",
    }
    risk_by_source = {
        "github": "需要核验 README、Demo 是否可运行，避免只因 star 高而误判产品价值。",
        "github_trending_daily": "GitHub Trending 代表今日注意力，不等同于真实采用率；需要结合 README、提交、release、issues 和 Demo 可用性判断。",
        "hf_spaces": "需要实际打开 Demo 体验，确认是否可用、速度是否稳定、输出是否可靠。",
        "hf_models": "需要确认模型许可、可用门槛和真实效果，避免仅凭下载量判断。",
        "hf_daily_papers": "需要核验论文结论是否被夸大，并转译为非技术用户能理解的表达。",
        "arxiv": "arXiv 论文未必经过充分验证，需要避免把研究设想写成确定事实。",
        "hackernews": "HN 讨论可能带有开发者圈偏见，需要结合产品实际体验再判断。",
    }
    return {
        "summary": summary[:80],
        "why_it_matters": why_by_source.get(item.source, "规则评分显示它近期热度、来源权重或互动指标较高，值得人工判断是否跟进。"),
        "xiaohongshu_angle": angle_by_type.get(content_type, "从“普通人能不能用、解决什么问题、是否值得尝试”的角度做一篇小红书图文。"),
        "content_type": content_type,
        "score": score,
        "risk": risk_by_source.get(item.source, "未生成 LLM 摘要，需要人工核验真实性和可用性。"),
    }


async def enrich_with_llm(item: SourceItemModel, score: float) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key or settings.llm_provider.lower() != "openai":
        return fallback_summary(item, score)
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        prompt = f"""
请基于下面 AI/科技热点候选，输出严格 JSON，不要 Markdown。
标题：{item.title}
来源：{item.source}
摘要：{item.summary or item.raw_text or ""}
链接：{item.url}
规则评分：{score}
JSON 字段：summary、why_it_matters、xiaohongshu_angle、content_type、score、risk。
content_type 只能是 ai_tool_review/github_project/paper_explainer/tech_news/product_discovery。
"""
        candidates = parse_model_candidates(settings.openai_model) or [settings.openai_model]
        resp = None
        last_error: Exception | None = None
        for index, model_name in enumerate(candidates):
            try:
                resp = await client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                break
            except Exception as exc:
                last_error = exc
                if index < len(candidates) - 1:
                    logger.warning("热点摘要模型 %s 调用失败，回退到 %s：%s", model_name, candidates[index + 1], exc)
                    continue
                raise

        if resp is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("LLM 摘要接口未返回结果")
        data = json.loads(resp.choices[0].message.content or "{}")
        fallback = fallback_summary(item, score)
        fallback.update({k: data.get(k, fallback[k]) for k in fallback})
        fallback["score"] = float(fallback.get("score") or score)
        return fallback
    except Exception:
        logger.exception("LLM 摘要失败，使用规则兜底：%s", item.title)
        return fallback_summary(item, score)
