# AI Radar V1

本仓库实现 `docs/AI_RADAR_V1_DEV.md` 规定的本地 AI/科技热点雷达后台。

## 功能

- FastAPI + SQLite + SQLAlchemy + APScheduler + Jinja2。
- 信息源 Adapter：GitHub、arXiv、HuggingFace Daily Papers、HuggingFace Models、HuggingFace Spaces、Hacker News。
- 统一 `SourceItem` 入库、去重、轻量聚类、规则评分。
- OpenAI LLM 摘要可选；未配置 `OPENAI_API_KEY` 时自动使用规则兜底。
- 页面：`/` 今日热点、`/sources` 来源状态、`/selected` 已选择、`/run-now` 手动触发。
- API：`GET /api/hotspots`、`GET /api/hotspots?status=selected`、`POST /api/hotspots/{id}/select|ignore|watch`。
- 研究子 Agent：在热点卡片点击“深度搜集”，先调用本地 Grok 接口联网搜集资料，再把资料包交给 Codex/OpenAI 兼容接口撰写最终深度报告。
- 博客 JSON 导出：生成 `latest.json`、`archive/{date}.json`、`items/{date}/{slug}.json`，供个人博客卡片区消费。
- 博客 A/B 层文字：导出时调用 OpenAI/Codex 兼容接口生成并缓存，页面访问只读缓存。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 <http://127.0.0.1:8000>，点击“手动采集”。

也可以命令行运行一次：

```bash
python scripts/run_once.py
```

只导出博客 JSON：

```bash
python scripts/export_blog_hotspots.py
```

跳过 A/B 层模型写作：

```bash
python scripts/export_blog_hotspots.py --skip-llm
```

强制重新生成 A/B 层写作缓存：

```bash
python scripts/export_blog_hotspots.py --force-analysis
```

## 环境变量

`GITHUB_TOKEN`、`HF_TOKEN` 推荐配置以获得更稳定的 API 配额，但不是强制。`OPENAI_API_KEY` 为空时不会调用 LLM，系统仍会生成规则评分热点。

写作模型相关配置支持候选列表，例如 `gpt-5.5,gpt-5.4`，会按顺序尝试；当前即使只填写 `gpt-5.5`，系统也会自动补充回退到 `gpt-5.4`。

研究子 Agent 默认配置：

```env
RESEARCH_AGENT_BASE_URL=http://127.0.0.1:40444/api/grok/v1/chat/completions
RESEARCH_AGENT_MODEL=grok-4.20-multi-agent-xhigh
RESEARCH_AGENT_TIMEOUT_SECONDS=180
```

该接口按 OpenAI Chat Completions 兼容格式调用，不需要 API Key。

C 层深度研究采用两段式流水线：

1. `RESEARCH_AGENT_*`：联网搜集资料、证据链接、时间线、争议和待验证点。
2. `OPENAI_*`：接收资料包并撰写最终博客 Markdown 报告。

博客导出配置：

```env
OPENAI_BASE_URL=http://47.250.164.154:8317/v1
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-5.5,gpt-5.4

BLOG_ANALYSIS_BASE_URL=
BLOG_ANALYSIS_MODEL=
BLOG_ANALYSIS_TIMEOUT_SECONDS=120
ENABLE_BLOG_ANALYSIS_LLM=true

PUBLIC_EXPORT_DIR=public_exports/ai-hotspots
BLOG_EXPORT_DIR=
BLOG_DETAIL_BASE_PATH=/ai-hotspots
DAILY_CARD_LIMIT=8
DAILY_EXPORT_LOOKBACK_HOURS=48
```

导出文件结构：

```text
public_exports/ai-hotspots/
├── latest.json
├── archive/
│   └── 2026-06-01.json
└── items/
    └── 2026-06-01/
        └── some-title-a1b2c3d4.json
```

博客接入 API：

- `POST /api/blog-export/today`：生成/更新 JSON 文件。
- `GET /api/blog-export/latest`：返回最新一期卡片索引。
- `GET /api/blog-export/items/{date}/{slug}`：返回单条详情。
- `POST /api/blog-export/items/{date}/{slug}/deep-research`：触发 C 层深度研究；已有成功报告时直接返回永久缓存。

## 数据库

默认 SQLite 文件位于 `data/radar.sqlite`。表结构包括：

- `source_items`
- `source_runs`
- `github_repo_snapshots`
- `hotspots`
- `hotspot_sources`
- `user_selections`
- `research_reports`

## 验证

```bash
python -m compileall app scripts
```
