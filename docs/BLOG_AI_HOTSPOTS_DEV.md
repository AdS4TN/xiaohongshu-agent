# 个人博客 AI/科技今日热点模块开发文档

> 面向后续 AI 开发 Agent / 工程实现者。  
> 本模块是 AI Radar 的“可嵌入个人博客”输出层，不是小红书发布流水线的一部分。

---

## 1. 产品目标

在用户已有个人博客中增加一个 AI/科技今日热点卡片区：

```text
A 层：首页 5-8 张热点小卡片
  ↓ 点击
B 层：默认详情分析页
  ↓ 感兴趣时
C 层：AI 深度研究报告
```

核心用途：

- 给站长自己做每日 AI/科技趋势阅读和选题判断。
- 给博客访客提供客观、中性的科技热点信息。
- 后续可被小红书内容 agent 复用，但公开博客展示不使用小红书风格。

---

## 2. 强约束

1. 第一版只使用当前已有免费/免费起步数据源：
   - GitHub
   - arXiv
   - HuggingFace Daily Papers
   - HuggingFace Models
   - HuggingFace Spaces
   - Hacker News
2. 不接入 X、Reddit、Product Hunt、微博、知乎、脉脉等来源。
3. 不做自动发布、不做评论回复、不做图片生成。
4. 公开博客内容必须是科技媒体风：
   - 客观
   - 中性
   - 克制
   - 不出现“我的判断”等第一人称表达
5. 小红书角度只保留为内部字段，不在公开博客默认展示。
6. 日期与归档时区固定使用 `Asia/Shanghai`，除非配置显式修改。
7. C 层深度研究：
   - 所有人都可以触发。
   - 第一个人触发后，其他人读取同一个缓存。
   - 成功结果永久缓存。
   - 不提供刷新接口。

---

## 3. 当前技术架构

```text
外部免费源
  ↓ adapters/*
source_items
  ↓ cluster/scoring/llm
hotspots
  ↓ Codex/OpenAI 兼容接口预生成 A/B 层文字
blog_analyses
  ↓ blog_export.py
public_exports/ai-hotspots/*.json
  ↓ 博客项目读取 JSON 或代理 Radar API
个人博客页面
  ↓ 用户点击“深度研究”
Radar API
  ↓ research_agent 联网搜集资料包
  ↓ Codex/OpenAI 兼容接口撰写最终 Markdown
research_reports
```

当前项目技术栈：

- FastAPI
- SQLite
- SQLAlchemy
- APScheduler
- Jinja2
- httpx

---

## 4. JSON 导出结构

标准目录：

```text
public_exports/ai-hotspots/
├── latest.json
├── archive/
│   └── 2026-06-01.json
└── items/
    └── 2026-06-01/
        ├── claude-opus-4-8-7f3a9c.json
        └── browser-use-a1b2c3d4.json
```

如果配置了 `BLOG_EXPORT_DIR`，系统会把 `public_exports/ai-hotspots/` 同步到博客项目指定目录。

### 4.1 `latest.json`

用于博客首页卡片区，只包含 A 层必要字段：

```json
{
  "schema_version": "ai-hotspots.v1",
  "timezone": "Asia/Shanghai",
  "date": "2026-06-01",
  "generated_at": "2026-06-01T09:00:00+08:00",
  "card_limit": 8,
  "cards": [
    {
      "id": 123,
      "slug": "example-ai-tool-a1b2c3d4",
      "report_date": "2026-06-01",
      "title": "Example AI Tool",
      "summary": "一句话摘要",
      "importance_score": 86.5,
      "source_tags": ["github", "hackernews"],
      "type": "github_project",
      "type_label": "开源项目",
      "detail_path": "/ai-hotspots/2026-06-01/example-ai-tool-a1b2c3d4",
      "item_json_path": "items/2026-06-01/example-ai-tool-a1b2c3d4.json",
      "deep_research_status": "not_started"
    }
  ]
}
```

### 4.2 `archive/{date}.json`

第一版与 `latest.json` 结构一致，用于历史归档页。

### 4.3 `items/{date}/{slug}.json`

用于 B 层详情页和 C 层缓存展示：

```json
{
  "id": 123,
  "slug": "example-ai-tool-a1b2c3d4",
  "report_date": "2026-06-01",
  "title": "Example AI Tool",
  "summary": "一句话摘要",
  "importance_score": 86.5,
  "source_tags": ["github"],
  "type": "github_project",
  "type_label": "开源项目",
  "detail_path": "/ai-hotspots/2026-06-01/example-ai-tool-a1b2c3d4",
  "detail": {
    "conclusion": "结论",
    "key_facts": ["事实 1", "事实 2"],
    "why_it_matters": "为什么重要",
    "impact": "影响面",
    "related_sources": [],
    "objective_analysis": "客观分析",
    "internal_xiaohongshu_angle": "内部字段，博客默认不要展示",
    "risk": "风险提示"
  },
  "deep_research": {
    "status": "not_started",
    "report_id": null,
    "model": null,
    "generated_at": null,
    "markdown": null,
    "sources": []
  }
}
```

---

## 5. API 约定

### 5.1 生成今日导出

```http
POST /api/blog-export/today
```

行为：

- 从数据库读取最新热点。
- 调用 Codex/OpenAI 兼容接口生成并缓存 A/B 层文字。
- 生成 `latest.json`、`archive/{date}.json`、`items/{date}/{slug}.json`。
- 不调用 C 层深度研究模型。

### 5.2 获取最新卡片索引

```http
GET /api/blog-export/latest
```

行为：

- 返回最新一期 A 层卡片 JSON。
- 也会确保导出文件存在。

### 5.3 获取单条详情

```http
GET /api/blog-export/items/{date}/{slug}
```

行为：

- 返回 B 层详情和当前 C 层缓存状态。
- `slug` 格式为标题 slug + 8 位 hash。

### 5.4 触发 C 层深度研究

```http
POST /api/blog-export/items/{date}/{slug}/deep-research
```

行为：

- 如果已有 `success` 报告，直接返回缓存，不重复生成。
- 如果已有 `running` 报告，返回 `already_running`。
- 如果没有成功/运行中报告，创建一次深度研究任务。

注意：

- 不提供刷新接口。
- 失败报告不会被当作永久缓存；后续再次触发可以重新尝试。

---

## 6. 环境变量

```env
TIMEZONE=Asia/Shanghai

OPENAI_BASE_URL=http://47.250.164.154:8317/v1
OPENAI_API_KEY=你的 key
OPENAI_MODEL=gpt-5.5

# A/B 层写作默认复用 OPENAI_*；如需单独覆盖可设置：
BLOG_ANALYSIS_BASE_URL=
BLOG_ANALYSIS_MODEL=
BLOG_ANALYSIS_TIMEOUT_SECONDS=120
ENABLE_BLOG_ANALYSIS_LLM=true

PUBLIC_EXPORT_DIR=public_exports/ai-hotspots
BLOG_EXPORT_DIR=
BLOG_DETAIL_BASE_PATH=/ai-hotspots
DAILY_CARD_LIMIT=8
DAILY_EXPORT_LOOKBACK_HOURS=48

RESEARCH_AGENT_BASE_URL=http://127.0.0.1:40444/api/grok/v1/chat/completions
RESEARCH_AGENT_MODEL=grok-4.20-multi-agent-xhigh
RESEARCH_AGENT_TIMEOUT_SECONDS=180
```

说明：

- `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`：用于热点摘要、A/B 层博客文案，以及 C 层最终报告撰写。
- `RESEARCH_AGENT_BASE_URL` / `RESEARCH_AGENT_MODEL`：只负责 C 层联网资料搜集包，不负责最终成稿。

- `PUBLIC_EXPORT_DIR`：AI Radar 项目内导出目录。
- `BLOG_EXPORT_DIR`：可选，博客项目静态数据目录。
- `BLOG_DETAIL_BASE_PATH`：博客详情页路径前缀。
- `DAILY_CARD_LIMIT`：首页卡片数量，默认 8。
- `DAILY_EXPORT_LOOKBACK_HOURS`：未指定日期时的候选热点回看窗口。

---

## 7. 命令行

采集并导出：

```bash
python scripts/run_once.py
```

只导出：

```bash
python scripts/export_blog_hotspots.py
```

跳过模型、只用规则兜底导出：

```bash
python scripts/export_blog_hotspots.py --skip-llm
```

强制重新调用 Codex 生成 A/B 层：

```bash
python scripts/export_blog_hotspots.py --force-analysis
```

按指定日期导出：

```bash
python scripts/export_blog_hotspots.py --date 2026-06-01
```

限制卡片数量：

```bash
python scripts/export_blog_hotspots.py --limit 5
```

---

## 8. 博客接入建议

第一版推荐博客自己路由和渲染：

1. 首页读取 `latest.json` 渲染 A 层卡片。
2. 详情页按 `/ai-hotspots/{date}/{slug}` 读取对应 item JSON。
3. “生成深度研究”按钮调用博客自己的 API Route。
4. 博客 API Route 再代理请求 Radar：

```http
POST http://127.0.0.1:8000/api/blog-export/items/{date}/{slug}/deep-research
```

这样 Radar 不需要直接暴露公网。

---

## 9. 后续可扩展但当前不要做

- 接入 X/KOL 列表
- 接入 Reddit
- 接入国内热榜
- 加向量库
- 自动生成小红书图文
- 自动发布
- 登录态采集
- 反爬绕过
- C 层刷新/多版本报告
