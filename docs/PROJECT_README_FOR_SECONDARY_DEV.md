# AI Radar 二次开发 README

这是一个本地运行的 AI/科技热点雷达项目，核心目标是：自动采集多源技术信息，筛选出值得关注的项目/新闻，再通过 AI 扩展资料、审核证据、生成可读摘要，最终服务于 GitHub 日榜、科技日报和个人博客内容生产。

当前项目已重点增强了 **GitHub Trending Daily 项目发现 → AI 信息扩展 → 摘要生成 → 前端展示** 这一条链路。

---

## 1. 项目能做什么

### 1.1 AI/科技热点雷达

- 采集 GitHub、Hacker News、arXiv、HuggingFace 等来源。
- 将不同来源统一入库为 `SourceItemModel`。
- 做去重、聚类、规则评分和 LLM 摘要。
- 在首页展示今日热点。

### 1.2 GitHub 今日创意项目雷达

页面：

```text
/github-daily
```

它会展示 GitHub Trending Daily 项目，并自动对每个项目执行 AI 信息探索：

```text
搜索 AI 第 1 轮
  ↓
审核 AI 第 1 轮
  ↓
搜索 AI 第 2 轮
  ↓
审核 AI 第 2 轮
  ↓
仓库结构读取 repo ingest
  ↓
摘要 AI 生成最终中文摘要
```

前端不需要手动触发。打开 `/github-daily` 后，缺失 AI 摘要的项目会自动进入后台队列。

### 1.3 Agent 原始返回调试

每个 GitHub 项目卡片中保留调试折叠区，可以看到：

- 搜索 AI 第 1 轮原始返回
- 审核 AI 第 1 轮原始返回
- 搜索 AI 第 2 轮原始返回
- 审核 AI 第 2 轮原始返回
- repo ingest 读取到的仓库结构和关键文件片段
- 摘要 AI 原始返回
- 每一步解析后的 JSON

这对调试提示词、判断来源质量、调整摘要风格非常重要。

---

## 2. 技术栈

```text
Backend        FastAPI
Database       SQLite + SQLAlchemy
Scheduler      APScheduler
Template       Jinja2
Frontend       服务端模板 + 原生 CSS/JS
HTTP Client    httpx
LLM Protocol   OpenAI-compatible Chat Completions
Repo Ingest    本地 mcp-git-ingest-local + GitHub API fallback
```

---

## 3. 目录结构

```text
.
├── app/
│   ├── main.py                         # FastAPI 入口、页面和 API 路由
│   ├── config.py                       # 环境变量配置
│   ├── database.py                     # SQLAlchemy engine/session
│   ├── models.py                       # 数据库表模型
│   ├── scheduler.py                    # 定时采集任务
│   ├── adapters/                       # 各信息源采集器
│   │   ├── github_trending.py          # GitHub Trending Daily 抓取
│   │   ├── github.py
│   │   ├── hackernews.py
│   │   ├── arxiv.py
│   │   └── huggingface.py
│   ├── services/
│   │   ├── ingest.py                   # 统一采集入口
│   │   ├── dedupe.py                   # 去重
│   │   ├── cluster.py                  # 聚类
│   │   ├── scoring.py                  # 规则评分
│   │   ├── hotspot.py                  # 热点生成和状态管理
│   │   ├── llm.py                      # 通用 LLM 摘要
│   │   ├── research_agent.py           # 深度研究资料包和报告
│   │   ├── daily_report.py             # AI/科技日报
│   │   ├── blog_export.py              # 博客 JSON 导出
│   │   ├── blog_analysis.py            # 博客 A/B 层结构化分析
│   │   ├── github_evidence.py          # GitHub 项目基础证据补全
│   │   └── github_info_expansion.py    # GitHub 项目信息扩展核心链路
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── github_daily.html           # GitHub 日榜页面
│   │   ├── raw_sources.html
│   │   ├── daily_report.html
│   │   └── ...
│   └── static/
│       ├── style.css
│       └── favicon.ico
├── scripts/
│   ├── run_once.py                     # 命令行运行一次采集
│   ├── enrich_existing.py              # 补全已有数据
│   ├── validate_github_info_expansion.py
│   └── ...
├── docs/
│   ├── GITHUB_TRENDING_DAILY_DEV.md
│   ├── GITHUB_INFO_EXPANSION_AI_PROMPTS.md
│   └── PROJECT_README_FOR_SECONDARY_DEV.md
├── data/
│   └── radar.sqlite                    # 默认 SQLite 数据库
├── public_exports/
│   └── ai-hotspots/                    # 博客导出 JSON
├── requirements.txt
├── .env.example
└── README.md
```

---

## 4. 快速启动

### 4.1 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

如果你使用系统 Python / Anaconda，也可以直接：

```powershell
pip install -r requirements.txt
```

### 4.2 配置环境变量

复制配置模板：

```powershell
copy .env.example .env
```

按需填写：

```env
GITHUB_TOKEN=
HF_TOKEN=

OPENAI_BASE_URL=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

GITHUB_INFO_SEARCH_BASE_URL=http://127.0.0.1:40444/api/grok/v1
GITHUB_INFO_SEARCH_MODEL=grok-4.20-multi-agent-xhigh
GITHUB_INFO_SEARCH_TIMEOUT_SECONDS=360

GITHUB_INFO_REVIEW_BASE_URL=http://47.250.164.154:8317/v1
GITHUB_INFO_REVIEW_API_KEY=你的 key
GITHUB_INFO_REVIEW_MODEL=gpt-5.5

GITHUB_INFO_SUMMARY_BASE_URL=http://47.250.164.154:8317/v1
GITHUB_INFO_SUMMARY_API_KEY=你的 key
GITHUB_INFO_SUMMARY_MODEL=gpt-5.5
GITHUB_INFO_WRITER_TIMEOUT_SECONDS=240

MCP_GIT_INGEST_LOCAL_PATH=C:/Users/28377/mcp-git-ingest-local
```

> 注意：不要把 `.env` 提交到 Git。

### 4.3 启动服务

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

打开：

```text
http://127.0.0.1:8010
http://127.0.0.1:8010/github-daily
```

### 4.4 手动运行一次采集

```powershell
python scripts\run_once.py
```

### 4.5 编译检查

```powershell
python -m compileall app scripts
```

---

## 5. 核心数据模型

模型定义在：

```text
app/models.py
```

### 5.1 SourceItemModel

所有原始来源统一进入这个表。

关键字段：

```text
id                  数据库主键
source              来源，例如 github_trending_daily
source_item_id      来源内部 ID，例如 owner/repo
title               标题
url                 原始 URL
author              作者
summary             简介
raw_text            原始文本
metrics_json        指标 JSON
raw_payload_json    原始 payload / 补充资料 JSON
content_hash        去重 hash
published_at
created_at
updated_at
```

GitHub 日榜相关数据通常存放在：

```text
metrics_json
raw_payload_json
```

例如：

```json
{
  "rank": 6,
  "stars_today": 317,
  "stars": 12345,
  "forks": 100,
  "language": "TypeScript",
  "topics": ["ai", "frontend", "design"]
}
```

AI 信息扩展结果写入：

```text
source_items.raw_payload_json.github_info_expansion
```

---

## 6. GitHub Trending Daily 链路

### 6.1 采集入口

```text
app/adapters/github_trending.py
```

负责抓取 GitHub Trending Daily 页面，解析：

- owner/repo
- title
- repo URL
- description
- language
- stars
- stars today
- forks
- rank

### 6.2 基础证据补全

```text
app/services/github_evidence.py
```

用于补充：

- GitHub API repo metadata
- topics
- README
- release
- pushed_at
- created_at
- license
- open issues

### 6.3 GitHub 日榜页面

```text
app/templates/github_daily.html
```

路由：

```text
GET /github-daily
```

实现位置：

```text
app/main.py -> github_daily_preview()
```

页面功能：

- 展示 GitHub Trending 项目列表。
- 展示 stars today、language、topics、README。
- 自动触发缺失项目的 AI 信息扩展。
- 展示最终摘要和 Agent 调试信息。

---

## 7. GitHub AI 信息扩展链路

核心文件：

```text
app/services/github_info_expansion.py
```

这是当前项目最重要的二次开发入口之一。

### 7.1 总体流程

```text
expand_and_save_github_project_info(db, item_id)
  ↓
expand_github_project_info(item)
  ↓
build_project_context(item)
  ↓
搜索 AI 第 1 轮
  ↓
抓取搜索来源网页内容片段 source_content_evidence
  ↓
审核 AI 第 1 轮
  ↓
搜索 AI 第 2 轮
  ↓
再次抓取网页内容片段
  ↓
审核 AI 第 2 轮
  ↓
repo ingest 读取仓库结构和关键文件
  ↓
摘要 AI 生成最终结构化中文摘要
  ↓
写回 raw_payload_json.github_info_expansion
```

### 7.2 三个 AI 的职责

#### 搜索 AI

配置：

```env
GITHUB_INFO_SEARCH_BASE_URL=http://127.0.0.1:40444/api/grok/v1
GITHUB_INFO_SEARCH_MODEL=grok-4.20-multi-agent-xhigh
```

职责：

- 联网搜索额外资料。
- 必须返回网页 URL。
- 必须使用全部可用 agent 并行搜索。
- 输出严格 JSON。

相关函数：

```python
build_search_messages(...)
```

#### 审核 AI

配置：

```env
GITHUB_INFO_REVIEW_BASE_URL=http://47.250.164.154:8317/v1
GITHUB_INFO_REVIEW_API_KEY=...
GITHUB_INFO_REVIEW_MODEL=gpt-5.5
```

职责：

- 不新增事实。
- 只审核搜索 AI 返回的事实是否被网页内容支撑。
- 判断网页内容是否和目标 GitHub 项目相关。
- 将事实分为 accepted / rejected / missing evidence。

相关函数：

```python
build_review_messages(...)
```

#### 摘要 AI

配置：

```env
GITHUB_INFO_SUMMARY_BASE_URL=http://47.250.164.154:8317/v1
GITHUB_INFO_SUMMARY_API_KEY=...
GITHUB_INFO_SUMMARY_MODEL=gpt-5.5
```

职责：

- 先看 repo ingest 读到的仓库结构和关键文件。
- 再结合审核通过的事实。
- 生成：
  - 一句话定义 `one_liner`
  - 项目用途
  - 火在哪
  - 创意点
  - 使用场景
  - 开发者启发
  - 最终日报摘要 `daily_report_paragraph`

相关函数：

```python
build_summary_messages(...)
```

---

## 8. repo ingest 机制

摘要 AI 不能只根据项目名和搜索结果猜用途，所以服务端会在摘要前读取仓库。

### 8.1 优先使用本地 Mcp-git-ingest MCP

本地路径：

```env
MCP_GIT_INGEST_LOCAL_PATH=C:/Users/28377/mcp-git-ingest-local
```

服务端会复用该 MCP 源码里的工具函数：

```text
git_directory_structure(repo_url)
git_read_important_files(repo_url, file_paths)
```

读取结果写入：

```text
github_info_expansion.repo_ingest_context
```

常见内容：

```json
{
  "status": "success",
  "tool": "mcp-git-ingest-local",
  "directory_tree": "...",
  "file_excerpts": [
    {
      "path": "README.md",
      "excerpt": "..."
    },
    {
      "path": "package.json",
      "excerpt": "..."
    }
  ]
}
```

### 8.2 GitHub API fallback

如果本地 MCP 读取失败，会 fallback 到 GitHub API：

- repo metadata
- recursive tree
- README / package.json / pyproject.toml / Cargo.toml / go.mod 等关键文件片段

fallback 结果中：

```json
{
  "tool": "github_api_repo_ingest_fallback"
}
```

---

## 9. 前端页面说明

### 9.1 首页

```text
GET /
```

模板：

```text
app/templates/index.html
```

展示博客热点卡片。

### 9.2 GitHub 日榜

```text
GET /github-daily
```

模板：

```text
app/templates/github_daily.html
```

特性：

- 自动触发 AI 信息扩展。
- 最多同时跑 2 个 GitHub 项目，避免模型接口过载。
- 如果还有项目在探索/等待，会每 30 秒自动刷新。
- 每个项目展示最终摘要。
- 调试区展示每个 Agent 的原始返回。

自动队列实现：

```text
app/main.py
  _github_info_running_ids
  _github_info_failed
  _ensure_github_info_expansion_queued(...)
  _run_github_info_expansion_worker(...)
```

### 9.3 原始数据查看

```text
GET /raw-sources
POST /api/raw-sources/query
```

用于调试不同来源的原始 payload。

---

## 10. 重要 API

### 10.1 手动采集

```http
POST /run-now
```

### 10.2 热点列表

```http
GET /api/hotspots
GET /api/hotspots?status=selected
```

### 10.3 选择/忽略/观察热点

```http
POST /api/hotspots/{hotspot_id}/select
POST /api/hotspots/{hotspot_id}/ignore
POST /api/hotspots/{hotspot_id}/watch
```

### 10.4 GitHub 项目信息扩展

```http
POST /api/source-items/{source_item_id}/github-info-expansion
```

注意这里的 `source_item_id` 是数据库里的 `source_items.id`，不是 `owner/repo`。

返回：

```json
{
  "status": "success",
  "source_item_id": 451,
  "expanded_at": "...",
  "models": {},
  "final_summary": {},
  "debug": {},
  "expansion": {}
}
```

### 10.5 博客导出

```http
POST /api/blog-export/today
GET /api/blog-export/latest
GET /api/blog-export/items/{date}/{slug}
POST /api/blog-export/items/{date}/{slug}/deep-research
```

---

## 11. 常用开发任务

### 11.1 调整 GitHub 日榜摘要风格

改这里：

```text
app/services/github_info_expansion.py
  build_summary_messages(...)
```

重点调整：

- `one_liner` 定义方式
- `daily_report_paragraph` 文风
- 是否写风险
- 是否写社区反馈
- 是否引用 repo ingest 结果

### 11.2 调整搜索策略

改这里：

```text
app/services/github_info_expansion.py
  build_search_messages(...)
```

可以调整：

- 搜索哪些网站
- 是否强制找二手来源
- 是否要求社区讨论
- 是否要求官网/文档/package registry

### 11.3 调整审核标准

改这里：

```text
app/services/github_info_expansion.py
  build_review_messages(...)
```

可以调整：

- 什么算“网页内容和项目相关”
- 什么事实可以接受
- README 自述是否可接受
- 社区反馈如何判定

### 11.4 调整自动队列并发数

改这里：

```text
app/main.py
  _ensure_github_info_expansion_queued(...)
```

默认：

```python
max_concurrent = 2
```

如果模型接口稳定，可以提高到 3 或 4。

### 11.5 重新生成某个项目摘要

```powershell
python scripts\validate_github_info_expansion.py --item-id 451
```

临时提高超时：

```powershell
$env:GITHUB_INFO_WRITER_TIMEOUT_SECONDS='600'
$env:GITHUB_INFO_SEARCH_TIMEOUT_SECONDS='600'
python scripts\validate_github_info_expansion.py --item-id 451
```

---

## 12. 当前 GitHub 信息扩展输出结构

写入位置：

```text
source_items.raw_payload_json.github_info_expansion
```

结构大致如下：

```json
{
  "version": "github_info_expansion_v1",
  "expanded_at": "2026-06-01T20:00:52Z",
  "models": {
    "search": "grok-4.20-multi-agent-xhigh",
    "review": "gpt-5.5",
    "summary": "gpt-5.5"
  },
  "project": {
    "source_item_id": "owner/repo",
    "title": "owner/repo",
    "url": "https://github.com/owner/repo"
  },
  "repo_ingest_context": {},
  "agent_outputs": {
    "round1_search": {
      "agent": "search",
      "model": "...",
      "raw_content": "..."
    },
    "round1_review": {},
    "round2_search": {},
    "round2_review": {},
    "final_summary": {}
  },
  "round1_search": {},
  "round1_review": {},
  "round2_search": {},
  "round2_review": {},
  "final_summary": {
    "project": "owner/repo",
    "one_liner": "...",
    "why_trending_today": "...",
    "what_it_does": [],
    "creative_angle": [],
    "user_scenarios": [],
    "developer_takeaways": [],
    "evidence_based_growth_reasons": [],
    "community_feedback": [],
    "risks_and_limitations": [],
    "verified_sources": [],
    "unverified_or_missing": [],
    "daily_report_paragraph": "..."
  }
}
```

---

## 13. 摘要文风目标

当前摘要 Agent 的目标不是写审计报告，也不是写营销文案。

更接近下面这种结构：

```text
一句话定义：
Impeccable 是一套开源 AI 前端设计技能包，用于改进生成式 UI 质量。

摘要：
Impeccable 是 pbakaus 开源的一套 AI 前端设计技能包，目标是让 Claude Code、Cursor、Codex CLI、Gemini CLI 等 AI 编码工具少生成同质化的“模板味”界面。它把设计知识拆成 7 个领域参考、23 条 /impeccable 命令和反模式检测规则，覆盖排版、色彩、动效、响应式、UX 文案等环节；CLI 和 Chrome 扩展还能在本地扫描页面，标出紫色渐变、卡片嵌套、低对比度等常见问题。项目今天进入 GitHub Trending Daily 第 6 位，当日新增 317 stars。对开发者来说，它值得看的不只是“美化 UI”，而是如何把领域知识、规则检测和 AI 工作流包装成可分发的专业工具。
```

核心要求：

- 第一句讲清“它是什么”。
- 摘要讲清“它干什么”。
- 再解释“火在哪”。
- 最后点出“为什么值得看”。
- 不要用“当前未观察到足够可验证外部反馈”这种审核口吻结尾。
- 不要用“神器、爆火、颠覆”等营销词。

---

## 14. 数据流总览

```text
外部信息源
  ├── GitHub Trending Daily
  ├── GitHub API
  ├── Hacker News
  ├── arXiv
  └── HuggingFace
      ↓
Adapter 采集
      ↓
SourceItem 入库
      ↓
去重 / 聚类 / 评分
      ↓
Hotspot / GitHub Daily 页面
      ↓
GitHub 项目 AI 信息扩展
      ├── 搜索 AI：联网找资料
      ├── 网页内容片段抓取：检查来源内容
      ├── 审核 AI：判断事实是否成立
      ├── repo ingest：读取仓库结构和关键文件
      └── 摘要 AI：生成项目定义和日报摘要
      ↓
前端展示 / 博客 JSON 导出 / 深度研究
```

---

## 15. 二次开发建议

### 15.1 优先稳定 GitHub 日榜

建议先围绕这条链路继续优化：

```text
GitHub Trending → repo ingest → AI 信息扩展 → 摘要 → 前端展示
```

原因：

- 数据质量相对可控。
- GitHub 项目天然适合用 repo ingest 判断用途。
- 最终内容容易产品化成日报、周报、项目榜单。

### 15.2 把提示词和代码进一步解耦

目前提示词写在：

```text
app/services/github_info_expansion.py
```

后续可以抽到：

```text
app/prompts/github_info/search.md
app/prompts/github_info/review.md
app/prompts/github_info/summary.md
```

这样更方便调试和版本管理。

### 15.3 给 AI 扩展任务加状态表

当前 GitHub 信息扩展结果直接写回 `source_items.raw_payload_json`。

后续如果要更稳定，建议新增表：

```text
github_info_expansion_jobs
```

字段可以包括：

```text
id
source_item_id
status
started_at
finished_at
error_message
round1_search_json
round1_review_json
round2_search_json
round2_review_json
repo_ingest_json
summary_json
agent_outputs_json
```

好处：

- 可以看到任务状态。
- 可以重试失败步骤。
- 不会把 `source_items.raw_payload_json` 撑得太大。

### 15.4 做最终日报排序

目前 GitHub Daily 是项目列表。

后续可以增加：

- AI 选 Top 10
- 按“新奇度 / 可用性 / 产品启发 / 社区热度”打分
- 自动生成日报 Markdown
- 导出到博客

---

## 16. 常见问题

### Q1：为什么有些项目一直显示“AI 探索中”？

可能是模型接口超时或仍在运行。

检查日志：

```text
logs/
```

也可以手动跑：

```powershell
python scripts\validate_github_info_expansion.py --item-id 451
```

### Q2：为什么摘要里还有英文？

项目名、包名、命令、README 原文、网页标题可能保留英文。自然语言字段已要求中文。

### Q3：为什么审核结果是 revise，但仍然生成摘要？

审核 AI 的 `revise` 表示部分来源或事实不够好，但摘要 AI 只会使用 `accepted_facts` 和项目上下文中可信的信息。这样可以避免因为某些弱来源导致整条摘要失败。

### Q4：为什么 repo ingest 有时失败？

可能原因：

- GitHub 网络超时。
- 仓库太大。
- 本地 `mcp-git-ingest-local` 缺依赖。
- 仓库不存在或私有。

失败时会 fallback 到 GitHub API 轻量读取。

### Q5：如何确认当前摘要是否使用了本地 MCP？

查看：

```text
source_items.raw_payload_json.github_info_expansion.repo_ingest_context.tool
```

如果是：

```text
mcp-git-ingest-local
```

说明走了本地 MCP 实现。

---

## 17. 当前推荐开发命令

```powershell
# 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8010

# 运行一次采集
python scripts\run_once.py

# 验证 GitHub Trending 解析
python scripts\validate_github_trending_parse.py

# 验证 GitHub 基础证据补全
python scripts\validate_github_evidence.py

# 验证单个 GitHub 项目信息扩展
python scripts\validate_github_info_expansion.py --item-id 451

# 编译检查
python -m compileall app scripts
```

---

## 18. 当前最重要的二次开发入口

如果你只想快速接手，优先看这几个文件：

```text
app/main.py
app/templates/github_daily.html
app/static/style.css
app/services/github_info_expansion.py
app/adapters/github_trending.py
app/services/github_evidence.py
app/models.py
app/config.py
```

其中最核心的是：

```text
app/services/github_info_expansion.py
```

它决定了 GitHub 项目最后写出来是什么质量。

