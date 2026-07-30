# API 说明

服务默认地址为 `http://127.0.0.1:8000`。FastAPI 交互式文档位于 `/docs`，OpenAPI JSON 位于 `/openapi.json`。

## 系统状态

### `GET /api/status`

返回运行状态、文章数量、启用配置数量、最近一次采集和下次定时时间。

## 采集任务

### `POST /api/collections`

启动一次手动采集，成功时返回 HTTP `202`：

```json
{
  "run_id": 12,
  "status": "running"
}
```

已有任务运行时返回 HTTP `409`。

### `GET /api/collections?limit=50`

获取采集运行日志，`limit` 范围为 1 至 200。

### `GET /api/collections/{run_id}`

获取一次采集的汇总和每个 RSS 源、关键词组的任务明细。

## 文章

### `GET /api/articles`

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string | 标题、摘要或发布方搜索词 |
| `source_id` | integer | RSS 源 ID |
| `keyword_id` | integer | 关键词组 ID |
| `limit` | integer | 返回数量，默认 50，最大 200 |
| `offset` | integer | 分页偏移量 |

每篇文章包含结构化的 `sources`、`keywords`、`languages`、`countries` 和 `categories` 字段。`sources` 中保存来源名称、Feed 或列表页地址、实际文章链接、GUID、语言、国家、分类及首次/最后发现时间。

## 数据源

### `GET /api/sources`

返回全部未归档数据源。`mode` 可以是 `search`、`direct` 或 `crawler`。每个来源还包含 `crawler_failure_kind`、`crawler_failure_count`、`crawler_cooldown_until`、`crawler_last_error`、`crawler_last_success_at` 和计算后的 `crawler_in_cooldown`，用于展示反爬与恢复状态。

### `POST /api/sources`

```json
{
  "name": "Google News 中文",
  "url_template": "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
  "mode": "search",
  "language": "zh-CN",
  "country": "CN",
  "active": true
}
```

`mode=search` 时 `url_template` 必须包含 `{query}`；`mode=direct` 时填写固定 Feed 地址；`mode=crawler` 时填写固定新闻列表页地址，不能包含 `{query}`。启用爬虫总开关后，直连或搜索源返回 HTML 而不是 XML 时，采集器才会自动使用网页爬虫兜底。

无 RSS 站点示例：

```json
{
  "name": "Example Policy News",
  "url_template": "https://example.com/news/",
  "mode": "crawler",
  "language": "en-US",
  "country": "US",
  "active": true
}
```

网页爬虫总开关默认关闭。开启后，爬虫仅跟踪同站 HTTP/HTTPS 链接，按链接特征优先读取文章页，单个数据源每次最多提取 30 篇带发布日期的文章。发布日期依次从 JSON-LD、页面元数据、`time` 元素和日期型 URL 中识别。爬虫默认遵守 `robots.txt`，同域请求间隔 3 秒；HTTP 429 优先采用 `Retry-After`，HTTP 403、5xx、验证码和持续解析失败会触发来源冷却。冷却期间不会发出爬虫请求，也不会推进该来源游标。

### `PUT /api/sources/{source_id}`

使用与新增相同的完整请求体更新数据源。

### `DELETE /api/sources/{source_id}`

归档数据源，不物理删除历史文章和日志。

## 关键词组

### `GET /api/keywords`

返回全部未归档关键词组。

### `GET /api/keyword-hit-stats`

返回全部关键词、各关键词分类及总体的命中效果。`hit_count` 是关键词初筛命中的去重候选文章数，`reviewed_count` 是已成功完成相关性审核的文章数，`relevant_count` 是审核确认真正相关的文章数，`pending_review_count` 是尚未成功完成审核的候选数。

`hit_rate` 按 `relevant_count / reviewed_count` 计算；没有已审核文章时返回 `null`，不会把待审核文章误算为无关。同一文章命中同一分类下多个关键词时，分类和总体汇总均只计一次。

### `POST /api/keywords`

```json
{
  "name": "一次性医疗手套",
  "category_id": 2,
  "match_terms": [
    "丁腈手套",
    "nitrile gloves",
    "medical gloves"
  ],
  "context_terms": ["关税", "需求", "tariff", "demand", "capacity"],
  "exclude_terms": ["boxing", "football", "baseball"],
  "lookback_days": 30,
  "require_local_match": true,
  "active": true
}
```

后端统一生成查询表达式，不接受客户端自定义 `query`。表达式格式为 `(主题词 OR …) AND (业务信号 OR …) -"排除词" when:30d`。表达式超过 200 字符时返回 `422`，应拆分成多个聚焦策略。采集时再依据 RSS 源的 `language` 切片：`zh-*` 只使用含汉字的词，`en-*` 只使用英文词；混合词组会分别生成中文和英文请求，无对应语言主题词的组合不会执行。

`require_local_match=true` 时，搜索型 RSS 返回项的标题或摘要还必须真实出现至少一个主题词，适合用在同义词更宽的拓展召回组以过滤搜索引擎误召回。修改主题词、业务信号词、排除词、回溯天数或该开关时，系统会清除该关键词的派生命中关系和采集游标，下一次采集按完整回溯窗口重新计算；历史文章和采集日志不会删除。

### `POST /api/keywords/preview`

请求体使用 `match_terms`、`context_terms`、`exclude_terms` 和 `lookback_days`，返回后端实际生成的 `query`，供页面实时预览。

### `PUT /api/keywords/{keyword_id}`

使用与新增相同的完整请求体更新关键词组。

### `DELETE /api/keywords/{keyword_id}`

归档关键词组。

## 采集设置

### `GET /api/settings`

返回每日采集时间、时区、增量和本地复核开关，以及爬虫总开关、robots.txt、限速和冷却配置。爬虫总开关默认关闭。

### `PUT /api/settings`

```json
{
  "schedule_time": "08:00",
  "incremental_collection": true,
  "search_local_keyword_filter": true,
  "crawler_enabled": false,
  "crawler_respect_robots": true,
  "crawler_min_interval_seconds": 3,
  "crawler_cooldown_minutes": 60
}
```

时间格式必须为 `HH:MM`，当前时区固定为 `Asia/Shanghai`。`crawler_enabled=false` 时仍正常读取 RSS，但显式爬虫源和 HTML 网页兜底不会发起爬虫请求。同域间隔范围为 0–60 秒，默认冷却时间范围为 5–1440 分钟。生产环境不建议关闭 robots.txt。

## AI 情报

### `GET /api/ai/status`

返回 DeepSeek 配置状态、模型、处理/日报运行状态，以及待处理、全文成功/失败、相关/无关、业务分析成功/失败数量、分类字典、阈值和三个 Prompt 版本。

### `POST /api/ai/analyze`

启动后台分析任务，返回 HTTP `202`：

```json
{
  "limit": 20,
  "process_all": true,
  "force": false,
  "refresh_content": false,
  "article_ids": null
}
```

默认 `process_all=true`：启动时锁定当时的全部可执行待办文章，并以 `limit` 指定的单批大小连续执行，直到该待办快照全部尝试完成；批次切换不需要再次调用接口。单篇失败不会阻断后续批次，也不会在本次任务内重复尝试。瞬时全文错误按 5、10 分钟指数退避，最多尝试 3 次；永久错误和达到上限的错误进入最终失败，不再扭曲待办数量。设置 `process_all=false` 可只处理最多 `limit` 篇。

任务依次执行 CCTQ GPT-5.4 mini 内置网页搜索读取、DeepSeek 相关性审核和真相关文章业务分析。`force=true` 会重新审核和分析已成功处理的文章；`refresh_content=true` 还会强制让 GPT 重新读取网页正文。`article_ids` 可指定文章 ID，最多 100 个。未配置 `OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY` 时返回 `503`，已有处理任务运行时返回 `409`。

响应中的 `article_count` 是本次锁定的文章总数，`batch_size` 是每个日志批次的文章上限。每个批次分别写入 `ai_analysis_runs`，便于查看阶段状态与 Token 用量。

### `GET /api/ai/articles`

| 参数 | 类型 | 说明 |
|---|---|---|
| `category` | string | 固定 AI 分类代码 |
| `date_from` | date | 新闻发布日期起点，北京时间 |
| `date_to` | date | 新闻发布日期终点，北京时间 |
| `limit` / `offset` | integer | 分页参数 |

该接口只返回全文审核通过且业务分析成功的最终业务新闻，同时包含 `final_url`、`content_chars`、相关性、摘要、分类、影响和风险字段。

### `GET /api/ai/reviews`

返回成功完成的正文相关性审核记录。支持 `relevant`、`date_from`、`date_to`、`limit` 和 `offset`，用于审计真相关与审核无关的理由、主分类 `category`、次分类 `secondary_categories`、证据、分数、模型及正文元数据。

### `GET /api/ai/runs?limit=50`

返回 AI 分析批次、成功/失败数量、相关/无关数量、模型、Prompt 版本和 Token 用量。

### `GET /api/ai/runs/{run_id}`

返回单个处理批次及每篇文章的 `content_status`、`relevance_status`、`business_analysis_status`、最终链接、正文字符数和错误信息。

### `GET /api/ai/content-failures`

返回全文读取失败列表，包括文章、错误分类、尝试次数、下一次重试时间、最终失败/等待重试/已忽略状态和错误原因。支持 `limit`、`offset`。

### `POST /api/ai/content-failures/{article_id}/retry`

重置该文章的退避与尝试次数并立即启动单篇 AI 处理任务。已有 AI 任务运行时返回 `409`，未配置 DeepSeek 时返回 `503`。

### `POST /api/ai/content-failures/{article_id}/ignore`

将失败文章标为已忽略，不再进入自动待办。已有 AI 任务运行时返回 `409`。

### `GET /api/ai/settings` 与 `PUT /api/ai/settings`

```json
{
  "business_profile": "英科医疗业务边界……",
  "relevance_prompt": "相关性审核的可编辑业务要求……",
  "report_prompt": "全部分类共用的日报生成要求……",
  "relevance_threshold": 70,
  "batch_size": 20,
  "parallelism": 4,
  "content_max_chars": 30000,
  "auto_analyze": false,
  "auto_report": false
}
```

企业业务边界、相关性提示词和统一日报提示词均可在系统设置中查看和修改。`parallelism` 控制正文抓取、相关性审核和业务分析各阶段的并发上限，范围为 1～20；三个阶段之间仍按顺序执行。系统会固定追加业务分类代码、JSON 输出结构、来源 ID/链接校验和防 Prompt 注入规则。自动开关默认关闭；`auto_report=true` 只有在 `auto_analyze=true` 时才会执行，并为当天生成一份综合日报。

## 数据维护

### `GET /api/maintenance/cleanup-preview`

参数 `scope` 可选 `failed_records`、`history`、`all_collected`。`history` 必须同时传入 `before=YYYY-MM-DD`。返回将删除的文章、采集日志、AI 日志和日报数量，不修改数据。

### `POST /api/maintenance/cleanup`

```json
{
  "scope": "history",
  "before": "2026-04-01",
  "confirmation": "DELETE"
}
```

必须精确提交 `confirmation=DELETE`。系统在删除前自动将 MySQL 逻辑备份到 `data/backups/`；采集、AI 分析或日报任务运行中返回 `409`。配置表、RSS 源和关键词组不在清理范围内。

## 情报日报

### `POST /api/reports`

按新闻发布日期生成后台综合日报任务：

```json
{
  "report_date": "2026-07-20"
}
```

一份日报汇总所选日期内全部通过相关性审核且业务分析成功的文章，不再按关键词分类过滤或拆分。所选日期没有合格新闻时返回 `422`。

### `GET /api/reports?limit=50`

返回已生成、运行中或失败的日报列表。

### `GET /api/reports/{report_id}`

返回日报结构化内容和全部依据文章。顶层包含经校验的 `sources`；兼容旧数据库时可能仍返回空的历史分类字段。`key_developments` 中每个业务方面包含小标题、`category`、`article_id` 与 `sources`；风险、机会、建议动作和监控清单中的每一项包含 `category`、`content`、`article_ids` 与 `sources`。这里的 `category` 是 AI 业务影响标签，不决定日报收录范围。后端会剔除不属于本次日报输入的来源 ID，再附加文章标题、发布方和 `source_url`。

## 错误约定

接口错误统一返回 FastAPI 的 `detail` 字段，例如：

```json
{
  "detail": "RSS 源名称或地址已存在"
}
```

常见状态码：`404` 资源不存在、`409` 配置冲突或任务正在运行、`422` 参数或业务范围校验失败、`503` DeepSeek Key 未配置、`500` 数据库错误。
