# AI Radar V1 开发文档

> 面向 AI 开发 Agent / 工程实现者的开发说明。  
> 本文档用于指导第一版“AI/科技热点雷达”模块开发，要求实现者严格按本文档范围和约束执行，不要自行扩大功能边界。

---

## 1. 项目背景

本项目服务于一个小红书 AI/科技垂直账号。

账号当前方向：

- AI 工具测评
- 有趣 AI 产品发现
- AI/科技圈热点转译
- 面向职场人、产品经理、程序员、创业者、学生、设计师和 AI 小白

第一阶段不做内容生成、不做图片生成、不做自动发布，只实现一个 **AI 热点监控后台**：

```text
免费信息源采集
  ↓
统一标准化
  ↓
去重与聚类
  ↓
热点评分
  ↓
LLM 摘要与小红书选题角度生成
  ↓
网页后台展示
  ↓
用户人工选择热点
  ↓
输出给后续内容生成模块
```

---

## 2. V1 核心目标

每天从免费或免费起步 API 中发现 AI/科技热点，并在后台展示给用户选择。

V1 成功标准：

1. 用户可以打开网页后台查看“今日 AI 热点”。
2. 系统可以从多个免费 API 拉取 AI/科技相关信息。
3. 系统可以去重、聚类、评分，并生成简短摘要。
4. 每个热点都给出“小红书可写角度”。
5. 用户可以将热点标记为：
   - 选择
   - 忽略
   - 观察
6. 被选择的热点可以通过 API 输出，供后续内容模块使用。

---

## 3. V1 范围

### 3.1 必须接入的信息源

V1 只接入以下免费/免费起步 API：

| 信息源 | 用途 | 是否需要 Token |
|---|---|---|
| GitHub | 发现 AI 开源项目、工具、Agent 框架、Star 增长项目 | 建议需要 `GITHUB_TOKEN` |
| arXiv | 发现 AI/LLM/Agent/多模态新论文 | 不需要 |
| HuggingFace Daily Papers | 发现社区筛选后的热门论文 | 建议需要 `HF_TOKEN` |
| HuggingFace Models | 发现新模型、热门模型、可测评模型 | 建议需要 `HF_TOKEN` |
| HuggingFace Spaces | 发现可体验 Demo、AI 工具、产品原型 | 建议需要 `HF_TOKEN` |
| Hacker News | 发现海外技术圈正在讨论的 AI 产品/工具 | 不需要 |

### 3.2 明确不做的内容

V1 不做以下内容：

- 不接入 X / Twitter API
- 不接入 Reddit
- 不接入 Product Hunt
- 不接入国内平台，如微博、知乎、脉脉
- 不做对标账号学习
- 不做自动生成小红书文案
- 不做图片生成
- 不做自动发布
- 不做评论回复
- 不做复杂向量数据库
- 不做登录态维护
- 不做浏览器自动化采集
- 不做反爬对抗

如需后续扩展，只能在 V1 完成并验收后新增。

---

## 4. 强约束

实现者必须遵守以下约束。

### 4.1 数据采集合规约束

必须优先使用官方 API 或公开结构化接口。

禁止：

- 绕过验证码
- 使用代理池规避限制
- 伪造设备指纹
- 抓取需要登录后才能查看的非公开内容
- 高频刷新网页
- 使用 Playwright/Selenium 模拟登录采集
- 保存第三方平台 Cookie

V1 只做低频、只读、公开信息监控。

### 4.2 API 使用约束

所有外部 API 调用必须：

- 设置超时时间
- 捕获异常
- 记录失败原因
- 单个来源失败不能影响其他来源
- 支持限速和重试
- 支持重复运行但不重复入库

### 4.3 成本约束

V1 数据源 API 必须免费或免费起步。

允许使用用户已有的大模型 API 做摘要和评分，但必须限量调用：

```text
每次采集最多对 Top 30 个候选热点调用 LLM
```

如果 LLM 调用失败，系统不能中断，应保留规则评分结果。

### 4.4 技术复杂度约束

V1 不引入：

- PostgreSQL
- Redis
- Celery
- Next.js
- LangGraph
- 向量数据库
- Docker 强依赖

V1 应保持本地可运行、结构清晰、便于后续扩展。

---

## 5. 推荐技术栈

```text
语言：Python 3.11+
后端：FastAPI
数据库：SQLite
ORM：SQLAlchemy 2.x
模板：Jinja2
HTTP 客户端：httpx
定时任务：APScheduler
配置管理：python-dotenv / pydantic-settings
数据校验：Pydantic
LLM 调用：OpenAI SDK 优先，其他模型后续适配
```

第一版以本地开发和本地运行优先。

---

## 6. 项目目录结构

建议实现以下目录结构：

```text
小红书agent/
├── app/
│   ├── main.py                  # FastAPI 入口
│   ├── config.py                # 配置读取
│   ├── database.py              # SQLite 初始化
│   ├── scheduler.py             # APScheduler 定时任务
│   ├── models.py                # SQLAlchemy 数据表
│   ├── schemas.py               # Pydantic Schema
│   ├── adapters/
│   │   ├── base.py              # BaseAdapter
│   │   ├── github.py            # GitHubAdapter
│   │   ├── arxiv.py             # ArxivAdapter
│   │   ├── huggingface.py       # HuggingFace adapters
│   │   └── hackernews.py        # HackerNewsAdapter
│   ├── services/
│   │   ├── ingest.py            # 采集入库
│   │   ├── dedupe.py            # 去重
│   │   ├── cluster.py           # 聚类
│   │   ├── scoring.py           # 规则评分
│   │   ├── llm.py               # LLM 摘要与评分
│   │   └── hotspot.py           # 热点生成
│   ├── templates/
│   │   ├── index.html           # 今日热点页
│   │   └── sources.html         # 数据源状态页
│   └── static/
│       └── style.css
├── data/
│   └── radar.sqlite
├── scripts/
│   └── run_once.py              # 手动跑一次采集
├── docs/
│   └── AI_RADAR_V1_DEV.md
├── .env.example
├── requirements.txt
└── README.md
```

---

## 7. 环境变量

`.env.example` 必须包含：

```env
# App
APP_ENV=development
APP_HOST=127.0.0.1
APP_PORT=8000
TIMEZONE=Asia/Shanghai

# Database
DATABASE_URL=sqlite:///data/radar.sqlite

# API Tokens
GITHUB_TOKEN=
HF_TOKEN=

# LLM
LLM_PROVIDER=openai
OPENAI_API_KEY=
MAX_LLM_CANDIDATES_PER_RUN=30

# Scheduler
ENABLE_SCHEDULER=true
DAILY_RUN_TIME=08:00
EVENING_RUN_TIME=18:00
```

说明：

- `GITHUB_TOKEN` 推荐配置，但不强制。
- `HF_TOKEN` 推荐配置，但不强制。
- 未配置 `OPENAI_API_KEY` 时，系统仍然可以运行，只是不生成 LLM 摘要。

---

## 8. 数据模型

### 8.1 source_items

保存所有原始采集条目。

字段：

```text
id                      integer primary key
source                  text        # github/arxiv/hf_daily_papers/hf_models/hf_spaces/hackernews
source_item_id           text        # 来源内唯一 ID
title                   text
url                     text
author                  text nullable
summary                 text nullable
raw_text                text nullable
published_at            datetime nullable
metrics_json            text        # JSON 字符串
raw_payload_json         text        # JSON 字符串
content_hash             text
created_at              datetime
updated_at              datetime
```

唯一约束：

```text
unique(source, source_item_id)
unique(content_hash)
```

### 8.2 source_runs

记录每次采集任务。

字段：

```text
id                      integer primary key
source                  text
status                  text        # success/failed/partial
started_at              datetime
finished_at             datetime nullable
items_fetched            integer
items_inserted           integer
error_message            text nullable
```

### 8.3 github_repo_snapshots

保存 GitHub 仓库 Star 快照，用于计算增长。

字段：

```text
id                      integer primary key
repo_full_name           text
stars                   integer
forks                   integer
open_issues              integer nullable
pushed_at                datetime nullable
snapshot_time            datetime
created_at               datetime
```

### 8.4 hotspots

保存聚类后的热点。

字段：

```text
id                      integer primary key
title                   text
summary                 text nullable
why_it_matters           text nullable
xiaohongshu_angle        text nullable
content_type             text        # ai_tool_review/github_project/paper_explainer/tech_news/product_discovery
score                   float
status                  text        # new/selected/ignored/watching
created_at              datetime
updated_at              datetime
```

### 8.5 hotspot_sources

保存热点与原始条目的关联。

字段：

```text
id                      integer primary key
hotspot_id               integer
source_item_id           integer
created_at               datetime
```

### 8.6 user_selections

记录用户对热点的操作。

字段：

```text
id                      integer primary key
hotspot_id               integer
selection_type           text        # selected/ignored/watching
note                    text nullable
created_at               datetime
```

---

## 9. 统一 SourceItem Schema

所有 Adapter 必须输出统一结构。

```python
class SourceItem(BaseModel):
    source: str
    source_item_id: str
    title: str
    url: str
    author: str | None = None
    summary: str | None = None
    raw_text: str | None = None
    published_at: datetime | None = None
    metrics: dict = {}
    raw_payload: dict = {}
```

禁止 Adapter 直接写热点表。

正确流程：

```text
Adapter
  → SourceItem
  → source_items 入库
  → 去重/聚类/评分服务
  → hotspots
```

---

## 10. Adapter 设计

### 10.1 BaseAdapter

所有信息源采集器必须继承同一接口：

```python
class BaseAdapter:
    source_name: str

    async def fetch(self) -> list[SourceItem]:
        raise NotImplementedError
```

要求：

- 每个 Adapter 只负责采集和标准化。
- 不负责 LLM 摘要。
- 不负责热点聚类。
- 不负责写入 `hotspots`。
- 请求必须设置 timeout。
- 异常必须向上抛出，由统一调度层记录 `source_runs`。

---

## 11. 各信息源采集要求

### 11.1 GitHubAdapter

目标：

```text
发现 AI 开源项目、AI 工具、Agent 框架、RAG 项目、MCP 项目、新模型应用。
```

推荐搜索关键词：

```text
llm
ai-agent
agent
rag
mcp
generative-ai
ai-tools
chatgpt
claude
gemini
openai
local-llm
multimodal
```

推荐使用 GitHub Search API。

每个仓库需要保存：

```text
full_name
description
html_url
homepage
stargazers_count
forks_count
open_issues_count
language
topics
created_at
updated_at
pushed_at
```

GitHub 评分重点：

- 当前 stars
- 24h star 增长
- 72h star 增长
- 是否近期更新
- 是否有 homepage/demo
- 是否有清晰 README
- 是否更像产品而不是纯代码库

第一天没有历史快照时：

- 不计算 star 增长
- 使用当前 stars、更新时间、homepage、描述质量做基础评分

### 11.2 ArxivAdapter

目标：

```text
发现 AI / LLM / Agent / RAG / 多模态 / 代码生成 / 视频生成方向新论文。
```

分类范围：

```text
cs.AI
cs.CL
cs.LG
cs.CV
stat.ML
```

关键词过滤：

```text
LLM
large language model
agent
RAG
retrieval augmented generation
multimodal
reasoning
tool use
AI assistant
code generation
video generation
robotics
```

要求：

- 使用 arXiv 官方 API。
- 请求必须低频。
- 连续请求之间应加入延迟。
- 每次运行每个分类最多抓取 100 条。

### 11.3 HFDailyPapersAdapter

目标：

```text
发现 HuggingFace 社区已经筛选出的热门论文。
```

采集范围：

- 当天 Daily Papers
- 前一天 Daily Papers 作为兜底

要求：

- 如果 HF Daily Papers 与 arXiv 论文重复，应在聚类阶段合并为一个热点。
- HF Daily Papers 来源权重高于普通 arXiv。

### 11.4 HFModelsAdapter

目标：

```text
发现近期热门或有增长潜力的新模型。
```

关注字段：

```text
model_id
author
likes
downloads
tags
pipeline_tag
last_modified
card_data
```

筛选倾向：

- 有明确用途的模型
- 可形成工具测评的模型
- 最近更新
- likes/downloads 较高
- 标签与 LLM、image、video、audio、agent、multimodal 相关

### 11.5 HFSpacesAdapter

目标：

```text
发现可直接体验的 AI Demo、产品原型和工具。
```

关注字段：

```text
space_id
author
likes
sdk
tags
last_modified
app_file
```

筛选倾向：

- 可交互 Demo
- 工具型 Space
- 图像、视频、音频、Agent、RAG、效率工具类
- 适合做小红书测评

### 11.6 HackerNewsAdapter

目标：

```text
发现海外技术圈正在讨论的新工具、新产品、开源项目。
```

采集范围：

```text
topstories
newstories
showstories
askstories
```

关键词过滤：

```text
AI
LLM
agent
OpenAI
Claude
Gemini
tool
startup
open source
machine learning
model
```

要求：

- 每次每类最多抓取 50 条详情。
- 先按标题过滤，再抓详情，减少请求量。

---

## 12. 热点生成流程

### 12.1 入库

每次运行：

1. 调用各 Adapter。
2. 得到 `SourceItem` 列表。
3. 计算 `content_hash`。
4. 去重写入 `source_items`。
5. 记录 `source_runs`。

`content_hash` 建议：

```text
sha256(source + source_item_id + normalized_url + normalized_title)
```

### 12.2 去重

去重优先级：

1. 相同 `source + source_item_id`
2. 相同 URL
3. 相同 GitHub repo full_name
4. 相同 arXiv paper id
5. 标题归一化后高度一致

### 12.3 聚类

V1 使用轻量聚类，不引入向量数据库。

聚类依据：

- GitHub repo 相同
- arXiv id 相同
- URL 域名和路径相同
- 标题关键词高度重合
- HF Daily Papers 与 arXiv paper id 匹配

同一热点可包含多个来源。

### 12.4 规则评分

所有候选先做规则评分。

基础维度：

```text
新鲜度
来源权重
互动指标
GitHub star 增长
是否有 demo/homepage
是否适合普通用户理解
是否适合小红书图文表达
```

来源权重建议：

```text
GitHub: 1.2
HuggingFace Spaces: 1.2
HuggingFace Daily Papers: 1.1
HuggingFace Models: 1.0
Hacker News: 1.0
arXiv: 0.9
```

内容类型倾向：

```text
工具/Demo/开源项目 > 社区讨论 > 论文
```

原因：

当前账号方向是 AI 工具测评和科技产品发现，小红书用户更容易理解工具和产品。

### 12.5 LLM 摘要与评分

只对规则评分 Top N 调用 LLM。

N 由环境变量控制：

```text
MAX_LLM_CANDIDATES_PER_RUN=30
```

LLM 必须输出 JSON：

```json
{
  "summary": "一句话摘要",
  "why_it_matters": "为什么值得关注",
  "xiaohongshu_angle": "适合小红书的切入角度",
  "content_type": "ai_tool_review",
  "score": 86,
  "risk": "需要验证工具是否真实可用"
}
```

字段约束：

- `summary`：不超过 80 个中文字符。
- `why_it_matters`：不超过 120 个中文字符。
- `xiaohongshu_angle`：必须是可写成小红书图文的角度。
- `score`：0-100。
- `risk`：可为空，但字段必须存在。

如果 LLM 返回非法 JSON：

- 记录错误
- 使用规则评分兜底
- 不阻断流程

---

## 13. Web 后台需求

### 13.1 今日热点页 `/`

展示今日热点卡片。

每张卡片必须包含：

- 标题
- 分数
- 内容类型
- 来源标签
- 摘要
- 为什么值得关注
- 小红书角度
- 风险提示
- 原始链接列表
- 操作按钮：
  - 选择
  - 忽略
  - 观察

排序：

```text
score 降序
created_at 降序
```

默认只展示最近 48 小时热点。

### 13.2 数据源状态页 `/sources`

展示每个来源最近一次运行状态：

- 来源名称
- 状态
- 开始时间
- 结束时间
- 抓取数量
- 入库数量
- 错误信息

### 13.3 手动触发 `/run-now`

提供按钮或接口，手动触发一次完整采集。

要求：

- 如果已有采集正在运行，不能重复启动。
- 返回任务已启动或正在运行提示。

### 13.4 已选择热点 `/selected`

展示用户标记为 `selected` 的热点。

用于后续内容模块读取。

### 13.5 JSON API

必须提供：

```text
GET /api/hotspots
GET /api/hotspots?status=selected
POST /api/hotspots/{id}/select
POST /api/hotspots/{id}/ignore
POST /api/hotspots/{id}/watch
```

`GET /api/hotspots?status=selected` 是后续模块的主要输入接口。

---

## 14. 调度策略

V1 使用 APScheduler。

默认调度：

```text
每天 08:00 运行一次完整采集
每天 18:00 运行一次补充采集
```

时区：

```text
Asia/Shanghai
```

手动触发：

```text
支持随时从后台执行一次
```

并发约束：

- 同一时间只允许一个完整采集任务运行。
- 不允许多个采集任务并发写 SQLite。

---

## 15. 错误处理

### 15.1 单源失败

如果某个 Adapter 失败：

- 记录 `source_runs.status = failed`
- 保存错误信息
- 继续执行其他 Adapter
- 页面展示该来源失败

### 15.2 API 限流

遇到 429：

- 当前来源停止本轮采集
- 记录限流错误
- 不重试超过 2 次
- 不影响其他来源

### 15.3 LLM 失败

如果 LLM API 调用失败：

- 使用规则评分兜底
- 热点仍然生成
- 摘要字段可为空或使用原始 summary
- 页面显示“未生成 LLM 摘要”

### 15.4 数据库错误

SQLite 写入失败时：

- 当前任务标记失败
- 打印日志
- 不吞掉异常

---

## 16. 日志要求

必须记录：

- 每次采集开始/结束
- 每个来源抓取数量
- 每个来源入库数量
- 去重数量
- 生成热点数量
- LLM 调用数量
- LLM 失败数量
- 错误堆栈

日志可以先输出到控制台，后续再接文件日志。

---

## 17. 安全要求

- `.env` 不允许提交到 Git。
- `GITHUB_TOKEN`、`HF_TOKEN`、`OPENAI_API_KEY` 不允许写死在代码中。
- 后台第一版只在本地运行，默认绑定 `127.0.0.1`。
- 不实现用户登录系统。
- 不对公网暴露服务。

---

## 18. 验收标准

### 18.1 功能验收

必须满足：

1. 可以安装依赖并启动 FastAPI 服务。
2. 可以打开首页查看热点列表。
3. 可以手动触发采集。
4. 至少 4 个信息源能成功采集并入库。
5. GitHub 项目可以生成 Star 快照。
6. arXiv 和 HF Daily Papers 的重复论文可以合并或至少不重复展示为高优先级。
7. LLM 可用时能生成摘要和小红书角度。
8. LLM 不可用时系统仍能展示规则评分热点。
9. 用户可以选择、忽略、观察热点。
10. `/api/hotspots?status=selected` 能返回已选择热点。

### 18.2 质量验收

热点列表应满足：

- 排名前列主要是 AI 工具、AI Demo、AI 开源项目、AI 热点论文。
- 明显无关内容不应进入 Top。
- 每条热点应能看出“为什么值得写”。
- 每条热点应至少包含一个原始来源链接。

### 18.3 稳定性验收

- 单个来源失败不影响整体任务。
- 重复运行不会大量重复入库。
- 采集任务不会无限重试。
- SQLite 数据库可在本地长期保存。

---

## 19. 建议开发顺序

### 阶段 1：项目骨架

实现：

- FastAPI 启动
- SQLite 初始化
- `.env` 配置读取
- 首页基础模板
- `/run-now` 占位接口

### 阶段 2：数据模型与入库

实现：

- `source_items`
- `source_runs`
- `hotspots`
- `hotspot_sources`
- `user_selections`
- 基础 CRUD

### 阶段 3：Adapter

按以下顺序实现：

1. GitHubAdapter
2. HFDailyPapersAdapter
3. ArxivAdapter
4. HFModelsAdapter / HFSpacesAdapter
5. HackerNewsAdapter

### 阶段 4：去重、评分、热点生成

实现：

- 内容 hash
- URL 去重
- GitHub repo 去重
- arXiv id 去重
- 规则评分
- 生成 hotspots

### 阶段 5：LLM 摘要

实现：

- Top N 候选摘要
- JSON 输出解析
- 失败兜底

### 阶段 6：后台操作

实现：

- 热点列表页
- 数据源状态页
- 已选择页
- 选择/忽略/观察按钮
- JSON API

### 阶段 7：定时任务

实现：

- APScheduler
- 每日 08:00
- 每日 18:00
- 防重复运行锁

---

## 20. 后续扩展预留

V1 代码结构要为后续扩展预留接口，但不要实现。

后续可能扩展：

```text
V2：手动导入 X 推文链接
V3：接入 X API
V4：接入内容生成 Agent
V5：接入图文分镜与卡片生成
V6：接入小红书发布包
V7：接入数据复盘
```

因此 V1 中热点输出 API 必须稳定：

```text
GET /api/hotspots?status=selected
```

后续内容生成模块只依赖这个接口。

---

## 21. 实现者注意事项

请严格遵守：

1. 不要擅自扩大信息源范围。
2. 不要引入 X、Reddit、国内平台。
3. 不要实现自动发帖。
4. 不要实现浏览器自动化采集。
5. 不要引入复杂基础设施。
6. 不要把 token 写死。
7. 不要让单个 API 失败拖垮全局任务。
8. 不要让 LLM 成为必需依赖。
9. 不要为了“智能”牺牲可调试性。
10. 所有 Agent/LLM 输出必须可追踪、可失败兜底。

---

## 22. 一句话总结

V1 要实现的是：

```text
一个本地运行的 AI 热点雷达后台：
从免费 API 采集 AI/科技信号，去重聚类后生成热点列表，
用少量 LLM 调用补充摘要和小红书角度，
最后让用户人工选择值得继续创作的热点。
```

V1 的关键词是：

```text
免费 API
低频采集
可追踪
可兜底
人工选择
后续可扩展
```

