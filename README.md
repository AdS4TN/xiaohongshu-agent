# AI Radar｜多源 AI 热点采集与研究助手

> 从分散的信息源中筛选 AI / 科技热点，完成资料聚合、研究报告生成和博客数据导出。

仓库名 `xiaohongshu-agent` 沿用早期命名。**当前实现是 AI 热点雷达与内容研究后台，不是小红书自动发布工具，不包含小红书登录、笔记发布或账号运营能力。**

## 项目定位

开源项目、论文和社区动态常常分散、重复且缺少上下文。本项目把采集、去重、聚类、筛选和研究串成一条流水线：先保留来源，再生成摘要和报告，最后供本地页面或博客消费。

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
信息源适配器 → SourceItem 入库 → 去重 / 聚类 / 评分 → 热点工作台
                                                    ├─ 人工筛选
                                                    ├─ 资料搜集 → 模型成稿 → 缓存
                                                    └─ JSON 导出 → 博客 / 其他前端
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
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 可选写作服务；模型以自己的服务支持情况为准 |
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

## 常用操作

```powershell
python scripts/run_once.py
python scripts/export_blog_hotspots.py --skip-llm
python scripts/export_blog_hotspots.py
python scripts/export_blog_hotspots.py --force-analysis
```

模型导出和研究可能产生费用。`--skip-llm` 跳过导出阶段模型写作。导出目录默认包含 `latest.json`、`archive/`、`items/`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/hotspots` | 查询热点 |
| POST | `/api/hotspots/{id}/select` | 选择热点；另有 `ignore` / `watch` |
| POST | `/api/hotspots/{id}/research` | 启动研究 |
| POST | `/api/blog-export/today` | 生成博客 JSON |
| GET | `/api/blog-export/latest` | 最新导出索引 |
| POST | `/api/blog-export/items/{date}/{slug}/deep-research` | 详情研究报告 |

完整接口以 `app/main.py` 和本地 `/docs` 为准。

## 目录与阅读顺序

```text
app/adapters/       信息源适配器
app/services/       采集、聚类、研究、日报与导出
app/models.py       数据模型
app/main.py         页面与 API
app/scheduler.py    调度
scripts/            手动运行与验证
integrations/       可选 Devvit 集成
.env.example        无真实凭据的配置模板
```

建议按 `app/adapters/base.py` → `app/models.py` → `app/services/ingest.py` → `app/main.py` 阅读。

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
