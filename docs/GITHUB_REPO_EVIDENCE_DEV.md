# GitHub 仓库证据增强层开发文档

## 背景与目标

`github_trending_daily` 已经能抓到 GitHub Trending 日榜上的仓库、排名、今日 Star 增长、语言、总 Star/Fork 等页面事实。但这些数据只能回答“今天谁上榜”，不能支撑 AI 判断：

- 项目到底做什么？
- README 是否讲清楚？
- 是否近期维护？
- license、release、topics、默认分支、open issues 等证据如何？
- 是否像空壳、镜像、fork、awesome-list、官方 docs 等噪声？

本阶段目标是新增 **GitHub Repo Evidence Enrichment**：对 GitHub 来源候选仓库补全 REST API 证据，并把证据写回 `SourceItemModel.summary/raw_text/metrics_json/raw_payload_json`，让后续 AI 打分和日报生成有足够上下文。

## 设计原则

1. 数据层只负责“补证据”，不做最终创意判断。
2. 增强逻辑必须幂等：同一个仓库多次增强只刷新字段，不重复创建 source item。
3. 不新增数据库表，先写回现有 `source_items`。
4. 不需要浏览器登录态；使用 `GITHUB_TOKEN` 时走 REST API，提高配额。
5. 严格控制请求数量，默认只增强新近候选或指定 limit。
6. README 作为后续 AI 判断的主要上下文保存；展示层只做折叠查看，不在数据层硬编码产品化信号规则。

## 适用来源

第一版增强这些 source：

- `github_trending_daily`
- `github`

判断仓库 full name：

- 优先 `source_item_id`，格式应为 `owner/repo`
- 否则从 `url` 解析

## API 数据来源

对每个仓库调用：

1. `GET https://api.github.com/repos/{owner}/{repo}`
   - 基础元数据、stars、forks、watchers、open issues、topics、license、created_at、pushed_at、default_branch、homepage、archived、fork、mirror_url 等。
2. `GET https://api.github.com/repos/{owner}/{repo}/readme`
   - README 内容，API 返回 base64，需要 decode。
   - 若 404，记录 `has_readme=false`，不视为整体失败。
3. `GET https://api.github.com/repos/{owner}/{repo}/languages`
   - 语言字节分布。
4. `GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=5`
   - 最新 release、release_count_sample、latest_release_at。
   - 若 404 或空列表，记录无 release。
5. `GET https://api.github.com/repos/{owner}/{repo}/commits?per_page=1`
   - 最新 commit 时间。

第一版不做：

- issues/PR 搜索计数，请求成本较高，后续再加。
- stargazers `starred_at`，分页成本高，后续用自建快照替代。

## 输出字段约定

### `metrics_json` 增量字段

保留原有字段，并 merge 以下字段：

```json
{
  "repo_created_at": "2025-01-01T00:00:00Z",
  "repo_updated_at": "2026-06-01T00:00:00Z",
  "repo_pushed_at": "2026-06-01T00:00:00Z",
  "default_branch": "main",
  "homepage": "https://...",
  "repo_description": "...",
  "topics": ["ai-agent", "llm"],
  "license": "mit",
  "is_fork": false,
  "is_archived": false,
  "is_template": false,
  "mirror_url": null,
  "watchers": 123,
  "subscribers": 45,
  "open_issues": 10,
  "network_count": 123,
  "has_readme": true,
  "readme_chars": 12345,
  "readme_truncated": true,
  "languages": {"Python": 12345},
  "primary_language_api": "Python",
  "latest_release_name": "v1.2.3",
  "latest_release_at": "2026-05-30T00:00:00Z",
  "release_count_sample": 5,
  "latest_commit_at": "2026-06-01T00:00:00Z",
  "evidence_enriched_at": "2026-06-01T00:00:00Z",
  "evidence_version": "github_repo_evidence_v2"
}
```

### `raw_payload_json` 增量字段

保留原始 payload，并加入：

```json
{
  "github_api_repo": {},
  "github_api_readme": {
    "name": "README.md",
    "path": "README.md",
    "html_url": "https://github.com/.../blob/main/README.md"
  },
  "github_api_languages": {},
  "github_api_releases_sample": [],
  "github_api_latest_commit": {},
  "readme_text": "README markdown 原文",
  "evidence_source_urls": []
}
```

### `summary/raw_text` 更新策略

- `summary`：优先使用 GitHub API `description`，为空再保留原 summary。
- `raw_text`：拼接：
  - full name
  - repo description
  - topics
  - README 原文
  - homepage
  - language/license/release 事实

这样 `enrich_with_llm()`、博客 A/B 层和 `daily_report` 都能读到 README 证据。产品化、创意度、传播性等判断交给后续 AI 评分层处理，不在采集层写死规则。

## 实现建议

新增文件：

- `app/services/github_evidence.py`

核心函数：

```python
async def enrich_github_source_items(db: Session, *, limit: int = 50, hours: int = 72, force: bool = False) -> dict:
    ...
```

职责：

1. 查询最近 `hours` 内 `source in ("github_trending_daily", "github")` 的 `SourceItemModel`。
2. 默认跳过已经有 `evidence_version == "github_repo_evidence_v1"` 的记录；`force=True` 时重新增强。
3. 对每个 item 调 GitHub API，merge metrics/raw_payload。
4. 更新 `summary/raw_text/metrics_json/raw_payload_json`。
5. 返回统计：`{"selected": n, "enriched": n, "skipped": n, "failed": n, "errors": [...]}`。

辅助函数建议：

```python
def repo_full_name_from_item(item: SourceItemModel) -> str | None: ...
def build_evidence_raw_text(...): ...
async def fetch_repo_evidence(client, full_name: str) -> dict: ...
```

## 接入调度

更新 `app/services/hotspot.py`：

在所有 adapter 跑完后、`generate_hotspots(db)` 前调用：

```python
from app.services.github_evidence import enrich_github_source_items

await enrich_github_source_items(db, limit=50, hours=72)
created = await generate_hotspots(db)
```

原因：先增强证据，再生成热点，LLM 才能读到 README 和 repo metadata。

## 验收标准

### 编译

```bash
python -m compileall app scripts
```

### 单仓库增强验证

建议提供脚本：

- `scripts/validate_github_evidence.py`

脚本逻辑：

1. 从 DB 查最近一条 `github_trending_daily` item。
2. 调 `enrich_github_source_items(limit=3, hours=168, force=True)`。
3. 打印前 3 条的：
   - `source_item_id`
   - `repo_created_at`
   - `repo_pushed_at`
   - `topics`
   - `has_readme`
   - `readme_chars`
   - `readme_saved_chars`
   - `raw_text` 长度

期望：

- `enriched > 0`
- 至少一条 `has_readme=true`
- `raw_payload_json.readme_text` 和 `raw_text` 明显包含 README 内容，长度通常 > 1000
- 不打印 token

## 错误处理

- 单个仓库失败不能中断整体增强。
- 403/429 时记录错误并停止后续请求，避免继续打 API。
- 404 readme/releases 不算失败，只记录缺失。
- JSON decode/base64 decode 错误只影响该字段，不影响基础 repo metadata。

## 安全与隐私

- 不需要浏览器登录态。
- 不读取用户 GitHub Cookie。
- 使用 `.env` 中 `GITHUB_TOKEN` 时仅作为 Authorization header，不写入日志、DB 或输出。
- README 是公开仓库内容，作为公开证据保存。

## 后续扩展

1. 用 GraphQL 一次性批量取 issue/PR/release/commit 计数，降低请求数。
2. 对 stargazer 做 sampled `starred_at`，估算真实今日增长。
3. 记录 repo snapshot，用本地差分替代页面 `stars_today`。
4. 对 README 做结构化摘要缓存，减少日报生成 token。
