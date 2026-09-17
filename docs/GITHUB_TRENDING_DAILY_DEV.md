# GitHub Trending Daily 采集开发文档

## 背景与目标

当前项目 `app/adapters/github.py` 主要通过 GitHub Search API 按 topic + stars 抓仓库，容易偏向历史高星老项目，不能稳定发现“今天被 GitHub 社区注意到的新鲜、有趣、创意项目”。

本迭代目标是新增一个 **GitHub Trending Daily** 数据入口：每天抓取 GitHub Trending 日榜，把页面上的榜单排名、今日增长、语言、简介等事实作为候选证据入库，后续再交给 AI 做创意度、实用性、产品启发性等主观评判。

> 采集层只负责“发现与留证”，不要在本层写复杂 AI 判断。

## 范围

### 本次必须实现

1. 新增 `GitHubTrendingDailyAdapter`。
2. 抓取公开页面：`https://github.com/trending?since=daily`。
3. 解析 Trending 页面中每个仓库卡片，生成 `SourceItem`。
4. 将该 Adapter 接入现有 `run_full_ingest()` 调度链路。
5. 为解析函数提供离线单元级验证入口，便于后续用 HTML fixture 测试。
6. 保持现有 `GitHubAdapter` 不破坏。

### 本次不做

1. 不做登录态抓取；第一版只用公开页面。
2. 不做浏览器自动化；优先 `httpx` 直接请求 HTML。
3. 不做 AI 打分和日报重写。
4. 不新增数据库表；先复用 `source_items.metrics_json/raw_payload_json` 留证。
5. 不依赖第三方非官方 Trending API。

## 数据口径

### SourceItem 映射

- `source`: 固定为 `github_trending_daily`
- `source_item_id`: 仓库 full name，例如 `owner/repo`
- `title`: 仓库 full name
- `url`: `https://github.com/{owner}/{repo}`
- `author`: owner
- `summary`: Trending 页面项目简介
- `raw_text`: 拼接 full name、简介、语言，便于后续检索/聚类
- `published_at`: 无法从 Trending 页面可靠获得，填 `None`
- `metrics`: 至少包含：
  - `rank`: 日榜排名，从 1 开始
  - `language`: 主语言，可能为空
  - `stars`: 页面展示总 star，整数，解析失败为 0
  - `forks`: 页面展示 fork，整数，解析失败为 0
  - `stars_today`: 页面展示今日新增 star，整数，解析失败为 0
  - `trending_since`: `daily`
  - `trending_url`: `https://github.com/trending?since=daily`
  - `snapshot_time`: UTC ISO 字符串
  - `discovery_source`: `github_trending_daily`
- `raw_payload`: 保存页面解析出的原始结构，至少包含：
  - `rank`
  - `full_name`
  - `description`
  - `language`
  - `stars_total`
  - `forks_total`
  - `stars_today`
  - `url`
  - `trending_url`

### 注意事项

1. GitHub Trending 不是官方 API，`stars_today` 只代表页面口径，不等同于我们自建快照差分。
2. HTML 结构可能变化，解析失败时应抛出清晰错误，让 `SourceRunModel` 记录失败。
3. 页面有时会返回空榜或被风控页面替代；如果解析到 0 个仓库，也应视为异常，避免静默成功。

## 解析策略

使用 `BeautifulSoup` 解析 HTML。

推荐选择器：

- 仓库卡片：`article.Box-row`
- 仓库链接：`h2 a`
- 简介：`p`
- 主语言：`[itemprop='programmingLanguage']`
- star 链接：`a[href='/{full_name}/stargazers']`
- fork 链接：`a[href='/{full_name}/forks']`
- 今日增长：在卡片全文里用正则匹配 `([\d,]+)\s+stars?\s+today`

解析函数应该尽量纯函数化：

```python
def parse_trending_html(html: str, *, snapshot_time: datetime | None = None) -> list[SourceItem]:
    ...
```

这样后续可以直接喂 HTML fixture 测试，不需要联网。

## 请求策略

- 使用 `httpx.AsyncClient`。
- 请求头：
  - `User-Agent`: 类似 `AI-Radar/1.0 (+https://github.com/trending)`
  - `Accept`: `text/html,application/xhtml+xml`
- 超时复用 `BaseAdapter.timeout`。
- 429 时抛出 `RuntimeError("github_trending_daily rate limited: HTTP 429")`。
- 非 2xx 调用 `raise_for_status()`。
- 每次抓取只请求一次 daily 页面，不要高频访问。

## 依赖

当前 `requirements.txt` 没有 BeautifulSoup，需要新增：

```txt
beautifulsoup4>=4.12.3
```

如果不想新增依赖，也可以用内置 `html.parser` + `BeautifulSoup` 仍然需要 bs4 包，所以建议显式加入。

## 接入点

1. 新增文件：
   - `app/adapters/github_trending.py`
2. 更新：
   - `app/adapters/__init__.py`
   - `app/services/hotspot.py`
3. 可选更新：
   - `app/services/scoring.py` 中为 `github_trending_daily` 添加 source weight，并让 `infer_content_type()` 返回 `github_project`。

## 验收标准

### 静态验收

```bash
python -m compileall app scripts
```

必须通过。

### 联网采集验收

在项目根目录执行一个最小脚本：

```bash
python - <<'PY'
import asyncio
from app.adapters.github_trending import GitHubTrendingDailyAdapter

async def main():
    items = await GitHubTrendingDailyAdapter().fetch()
    print(len(items))
    for item in items[:5]:
        print(item.source, item.source_item_id, item.metrics)

asyncio.run(main())
PY
```

期望：

- 返回数量大于 0，通常约 25 条。
- 前 5 条 `source` 均为 `github_trending_daily`。
- `metrics.rank` 从 1 递增。
- `metrics.stars_today`、`metrics.stars`、`metrics.forks` 为整数。

### 入库验收

可以通过现有 `run_adapter()` 跑单个 adapter：

```bash
python - <<'PY'
import asyncio
from app.adapters.github_trending import GitHubTrendingDailyAdapter
from app.database import SessionLocal, init_db
from app.services.ingest import run_adapter

async def main():
    init_db()
    db = SessionLocal()
    try:
        print(await run_adapter(db, GitHubTrendingDailyAdapter()))
    finally:
        db.close()

asyncio.run(main())
PY
```

期望：

- `items_fetched > 0`
- `SourceRunModel.source == github_trending_daily` 有成功记录
- `source_items` 中能查到 `source='github_trending_daily'` 的记录

## 后续扩展方向

1. 再用 GitHub REST API 对 Trending 候选补 README、topics、license、release、commits。
2. 增加 `weekly/monthly` 作为辅助口径。
3. 增加 Python/TypeScript/Go 等语言榜候选池。
4. 自建 Star 快照差分，避免完全依赖页面 `stars today`。
5. 加 HTML fixture，防止 GitHub DOM 变化导致解析静默失效。
