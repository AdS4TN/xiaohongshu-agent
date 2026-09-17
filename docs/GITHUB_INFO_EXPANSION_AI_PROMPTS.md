# GitHub 项目信息扩展 AI 提示词与流水线

## 目标

对每个 GitHub 候选项目执行：

1. 搜索 AI 联网搜集额外信息，要求必须有网页 URL 返回。
2. 服务端抓取搜索 AI 返回 URL 的网页标题、meta 描述和正文片段。
3. 审核 AI 核对网页内容是否与目标 GitHub 项目相关，以及是否能支撑搜索 AI 的事实。
4. 搜索 AI 基于审核意见做第二轮延伸搜索。
5. 服务端再次抓取第二轮 URL 的网页内容片段。
6. 审核 AI 再次核对相关性和证据支撑。
7. 摘要 AI 只综合审核通过的信息，生成日报可用摘要。

## 模型配置

搜索 AI：

- base URL：`GITHUB_INFO_SEARCH_BASE_URL`
- model：`GITHUB_INFO_SEARCH_MODEL`
- 不使用 API key
- 默认：`grok-4.20-multi-agent-xhigh`
- 提示词强制要求“使用全部可用 agent 并行搜索”。

审核 AI：

- base URL：`GITHUB_INFO_REVIEW_BASE_URL`
- API key：`GITHUB_INFO_REVIEW_API_KEY`
- model：`GITHUB_INFO_REVIEW_MODEL`

摘要 AI：

- base URL：`GITHUB_INFO_SUMMARY_BASE_URL`
- API key：`GITHUB_INFO_SUMMARY_API_KEY`
- model：`GITHUB_INFO_SUMMARY_MODEL`

密钥写入 `.env`，不要写入代码或日志。

## 实现位置

- 服务：`app/services/github_info_expansion.py`
- 验证脚本：`scripts/validate_github_info_expansion.py`
- API：`POST /api/source-items/{source_item_id}/github-info-expansion`

## 三个 AI 的职责

### 搜索 AI

输入：项目标题、GitHub URL、README、GitHub metadata、上一轮审核结果。

输出严格 JSON：

- `sources`：网页来源列表，每条有 URL。
- `verified_facts`：可由网页来源支持的事实。
- `community_signals`：社区反馈。
- `adoption_or_product_signals`：产品化/采用信号。
- `novelty_and_creativity_notes`：创意点。
- `risks_and_limitations`：风险限制。
- `open_questions`：下一轮问题。
- `next_search_directions`：下一轮搜索方向。

### 审核 AI

输入：项目上下文、搜索包、服务端抓取的网页内容片段、历史搜索包。

输出严格 JSON：

- `verdict`：`pass` 或 `revise`
- `source_audit`：逐条来源是否与项目相关、是否能支撑 claim。
- `accepted_facts`：审核通过事实。
- `rejected_or_rewrite_needed`：拒绝或需要改写的内容。
- `missing_evidence`：缺失证据。
- `next_round_questions`：下一轮必须补查的问题。

审核时需要参考 `source_content_evidence`：

- 必须查看对应 source_id 的 `title`、`meta_description`、`text_excerpt`。
- 页面片段里没有项目名、仓库名、作者/组织名、官网域名、包名、论文名或其他强关联线索时，来源应标为 `unrelated` 或 `weak`。
- 页面片段和项目相关但不足以证明 claim 的具体表述时，必须拒绝或要求改写。
- 抓取失败、403/429、空正文只能作为 `weak/needs_manual_check`，不能支撑高置信事实。

### 摘要 AI

输入：项目上下文、两轮搜索包、两轮审核结果。

输出严格 JSON：

- `one_liner`
- `why_trending_today`
- `what_it_does`
- `creative_angle`
- `user_scenarios`
- `developer_takeaways`
- `evidence_based_growth_reasons`
- `community_feedback`
- `risks_and_limitations`
- `verified_sources`
- `unverified_or_missing`
- `daily_report_paragraph`

## 验证

```powershell
python scripts\validate_github_info_expansion.py --item-id 446
```

不传 `--item-id` 时默认取最新 GitHub Trending 第一条。

运行结果会写回 `source_items.raw_payload_json.github_info_expansion`，并在 `metrics_json` 中写入：

- `github_info_expansion_version`
- `github_info_expanded_at`
