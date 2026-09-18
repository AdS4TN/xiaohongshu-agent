# AI Radar｜热点驱动的博客与文章写作助手

> 自动发现 AI / 科技热点，围绕选题搜集资料、生成文章，并输出可供博客展示的内容数据。

仓库名 `xiaohongshu-agent` 沿用早期命名，项目主要服务于个人博客和科技文章创作。

## 项目定位

写科技博客需要持续寻找选题、查阅来源、组织材料，再把零散信息写成文章。本项目围绕这一创作流程，将热点发现、资料搜集、模型写作和博客内容导出串起来，减少重复收集和整理资料的工作。

**热点采集是选题入口，博客与文章生成才是核心用途。** 系统既能围绕单条热点生成短文，也能进一步搜集资料形成深度内容，或汇总多条热点生成日报。生成结果保留来源，供人工核对后接入博客。

## 从热点到文章

1. **发现选题**：采集开源项目、论文、产品和社区动态，去重、聚类并评分。
2. **筛选热点**：在工作台查看来源与摘要，选择需要关注和展开的内容。
3. **准备材料**：使用已有热点信息，或通过研究服务补充背景、事实与来源链接。
4. **生成文章**：调用写作模型，输出标题、摘要、单热点短文或深度研究内容。
5. **缓存与导出**：保存生成结果，导出博客卡片、文章详情和归档 JSON。
6. **审核与展示**：核对事实与引用，由自己的博客读取导出内容；不等同于自动登录内容平台发布文章。

### 内容产物

| 产物 | 内容 | 适用场景 |
| --- | --- | --- |
| 热点卡片 | 标题、一句话摘要、类别与热点信号 | 博客首页、资讯列表 |
| 单热点文章 | 标题与中文短文；当前提示词要求 400–800 字、3–5 个自然段 | 热点解读、开源项目或产品介绍 |
| 深度内容 | 资料搜集后生成的 Markdown 报告与来源链接 | 需要展开背景与细节的文章 |
| 热点日报 | 汇总多条热点，组织为当日内容 | 博客日报与阶段性资讯整理 |
| 博客数据 | 最新索引、按日归档、单篇详情 JSON | 接入自己的博客前端 |

字数和段落要求属于生成提示约束，不是对每次模型输出的硬性保证。文章写作需要配置模型服务；未配置时仍可采集热点并导出规则兜底内容，但不能等同于完成模型成稿。

## 已实现能力

| 模块 | 实现内容 | 条件 |
| --- | --- | --- |
| 多源采集 | GitHub、GitHub Trending、arXiv、Hugging Face、Hacker News 适配器 | 对应外部服务可访问 |
| 热点处理 | 统一 SourceItem、去重、轻量聚类、规则评分 | 不要求模型密钥 |
| 本地工作台 | 热点列表、来源状态、选择 / 忽略 / 关注 | FastAPI + Jinja2 |
| 模型写作 | 可选摘要、博客 A/B 层文字、深度报告 | 自行配置兼容模型服务 |
| 深度研究 | 资料搜集与成稿分离，保存来源与研究结果 | 额外研究服务，不随仓库提供 |
| 项目扩展 | GitHub 资料扩展、社区信号收集、日报生成 | 各功能有独立外部依赖 |
| 博客输出 | JSON 索引、归档、详情及生成结果缓存 | 可直接读取导出文件 |
| 调度 | APScheduler 定时任务与手动触发 | 可关闭自动调度 |

## 架构与技术栈

```text
多源热点采集 → 去重 / 聚类 / 评分 → 选题与来源材料
                                      ├─ 单热点短文写作
                                      ├─ 补充资料 → 深度文章
                                      └─ 多热点汇总 → 日报
                                                ↓
                                      生成结果缓存与来源留存
                                                ↓
                                     JSON 导出 → 审核 → 博客展示
```

- 后端：Python、FastAPI、Pydantic、SQLAlchemy、SQLite。
- 调度与采集：APScheduler、HTTPX、Beautiful Soup；可选浏览器采集使用 Playwright。
- 展示与集成：Jinja2、JSON 导出、兼容模型接口、可选 Reddit Devvit 桥接。
- 工程关注点：来源适配与业务处理分离、规则兜底、结果缓存、配置与运行数据隔离。

## 快速开始

以下以 Python 3.11+ 和 PowerShell 为例，依赖版本范围见 `requirements.txt`：

```powershell
git clone https://github.com/AdS4TN/xiaohongshu-agent.git
cd xiaohongshu-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:ENABLE_SCHEDULER = "false"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>，手动触发采集。首次启动没有历史热点；来源不可达或限流时不保证完整结果。确认配置后，可将 `.env` 中 `ENABLE_SCHEDULER` 设为 `true`，取消同名环境变量覆盖并重启。

### 配置分层

| 配置 | 用途 |
| --- | --- |
| `DATABASE_URL` | 默认 `sqlite:///data/radar.sqlite` |
| `GITHUB_TOKEN` / `HF_TOKEN` | 可选，提高对应来源的访问配额 |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 文章生成使用的模型服务；模型以自己的服务支持情况为准 |
| `ENABLE_BLOG_ANALYSIS_LLM` | 博客分析是否使用模型 |
| `RESEARCH_AGENT_*` | 外部资料搜集服务 |
| `GITHUB_INFO_SEARCH_*` / `GITHUB_INFO_REVIEW_*` / `GITHUB_INFO_SUMMARY_*` | 项目资料扩展的服务配置 |
| `PUBLIC_EXPORT_DIR` / `BLOG_EXPORT_DIR` | 导出路径 / 可选博客同步路径 |

**先运行无模型的基础采集，再接研究和写作服务。** 无模型密钥时，基础热点流程保留规则处理能力，不代表所有扩展接口都能离线运行。示例中的本地网关不是内置服务；使用扩展前应显式配置自己的地址与凭据，不要依赖源码中的开发环境默认地址。

需要 Reddit 浏览器采集时，再安装浏览器并准备自己的登录态：

```powershell
python -m playwright install chromium
python scripts/reddit_browser_login.py
```

## 生成第一批博客内容

完成基础启动后，在 `.env` 配置写作服务的 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`，并启用 `ENABLE_BLOG_ANALYSIS_LLM`。先采集热点，再调用文章生成与导出：

```powershell
python scripts/run_once.py
python scripts/export_blog_hotspots.py
```

生成结果会缓存，方便博客重复读取，不需要每次访问文章时重新调用模型。需要刷新已有文章内容时执行：

```powershell
python scripts/export_blog_hotspots.py --force-analysis
```

如果只想验证采集与导出、不调用写作模型：

```powershell
python scripts/export_blog_hotspots.py --skip-llm
```

模型写作和研究可能产生费用。默认导出目录为 `public_exports/ai-hotspots/`，包括 `latest.json`、`archive/`、`items/`。博客前端可以读取这些 JSON，或配置 `BLOG_EXPORT_DIR` 同步到自己的博客数据目录；这一步不是自动发布到第三方平台。

深度文章还需要配置 `RESEARCH_AGENT_*`。上面的常规导出不会自动完成每条热点的深度研究，可通过详情研究接口另行触发。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/hotspots` | 查询热点 |
| POST | `/api/hotspots/{id}/select` | 选择热点；另有 `ignore` / `watch` |
| POST | `/api/hotspots/{id}/research` | 启动研究 |
| POST | `/api/blog-export/today` | 生成博客 JSON |
| GET | `/api/blog-export/latest` | 最新导出索引 |
| POST | `/api/daily-report/today` | 生成热点日报 |
| POST | `/api/blog-export/items/{date}/{slug}/deep-research` | 详情研究报告 |

完整接口以 `app/main.py` 和本地 `/docs` 为准。

## 目录与阅读顺序

```text
app/adapters/       信息源适配器
app/services/       采集、研究、文章写作、日报与导出
app/models.py       数据模型
app/main.py         页面与 API
app/scheduler.py    调度
scripts/            手动运行与验证
integrations/       可选 Devvit 集成
.env.example        无真实凭据的配置模板
```

建议先读内容主线：`app/services/blog_analysis.py`（短文写作）→ `app/services/research_agent.py`（资料搜集与深度内容）→ `app/services/daily_report.py`（日报）→ `app/services/blog_export.py`（博客输出）。采集入口再参考 `app/adapters/base.py` 和 `app/services/ingest.py`。

## 验证与边界

本地核验（2026-09-18，Python 3.12）：语法检查与内置 GitHub Trending HTML 样例解析通过。未执行真实采集、模型调用或部署验收。

```powershell
python -m compileall -q app scripts
python scripts/validate_github_trending_parse.py
```

解析验证使用内置 HTML 样例，不代表真实来源始终可用。`scripts/smoke_acceptance.py` 依赖已经采集的数据，不能作为空库首次启动测试。

- 当前为本地工具，没有完整多用户权限与生产隔离机制，默认只监听回环地址。
- 网页结构、配额、登录态和模型返回会影响结果；研究报告需回看来源并人工核对。
- 不宣称生产 SLA、采集覆盖率或未经测量的性能指标。
- 仓库是整理后的源码快照，不包含个人密钥、数据库、浏览器登录态和历史日志。

文档：[开发说明](docs/AI_RADAR_V1_DEV.md) · [站点需求](docs/AI_HOTSPOTS_SITE_PRD.md) · [写作约束](docs/AGENT_WRITER.md)。

使用前确认来源访问规则与内容使用权限。不要提交 `.env`、Cookie 或数据库。仓库未附独立 LICENSE 文件，不额外声明开源授权。
