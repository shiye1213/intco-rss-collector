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

每篇文章包含结构化的 `sources`、`keywords`、`languages`、`countries` 和 `categories` 字段。`sources` 中保存来源名称、Feed 地址、RSS 提供的文章链接、GUID、语言、国家、分类及首次/最后发现时间。

## RSS 源

### `GET /api/sources`

返回全部未归档 RSS 源。

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

`mode=search` 时 `url_template` 必须包含 `{query}`；`mode=direct` 时填写固定 Feed 地址。

### `PUT /api/sources/{source_id}`

使用与新增相同的完整请求体更新 RSS 源。

### `DELETE /api/sources/{source_id}`

归档 RSS 源，不物理删除历史文章和日志。

## 关键词组

### `GET /api/keywords`

返回全部未归档关键词组。

### `POST /api/keywords`

```json
{
  "name": "PE 手套",
  "query": "",
  "match_terms": [
    "PE手套",
    "聚乙烯手套",
    "polyethylene gloves"
  ],
  "active": true
}
```

后端会忽略客户端传入的 `query` 内容，统一根据 `match_terms` 生成查询表达式。

### `PUT /api/keywords/{keyword_id}`

使用与新增相同的完整请求体更新关键词组。

### `DELETE /api/keywords/{keyword_id}`

归档关键词组。

## 采集设置

### `GET /api/settings`

返回每日采集时间和时区。

### `PUT /api/settings`

```json
{
  "schedule_time": "08:00"
}
```

时间格式必须为 `HH:MM`，当前时区固定为 `Asia/Shanghai`。

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

默认 `process_all=true`：启动时锁定当时的全部待办文章，并以 `limit` 指定的单批大小连续执行，直到该待办快照全部尝试完成；批次切换不需要再次调用接口。单篇失败不会阻断后续批次，也不会在本次任务内重复尝试，失败项会保留为待办供下次重试。设置 `process_all=false` 可保留旧行为，只处理最多 `limit` 篇。

任务依次执行最终链接解析、网页正文抽取、相关性审核和真相关文章业务分析。`force=true` 会重新审核和分析已成功处理的文章；`refresh_content=true` 还会强制重新抓取网页正文。`article_ids` 可指定文章 ID，最多 100 个。未配置 `DEEPSEEK_API_KEY` 时返回 `503`，已有处理任务运行时返回 `409`。

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

返回成功完成的正文相关性审核记录。支持 `relevant`、`date_from`、`date_to`、`limit` 和 `offset`，用于审计真相关与审核无关的理由、证据、分数、模型及正文元数据。

### `GET /api/ai/runs?limit=50`

返回 AI 分析批次、成功/失败数量、相关/无关数量、模型、Prompt 版本和 Token 用量。

### `GET /api/ai/runs/{run_id}`

返回单个处理批次及每篇文章的 `content_status`、`relevance_status`、`business_analysis_status`、最终链接、正文字符数和错误信息。

### `GET /api/ai/settings` 与 `PUT /api/ai/settings`

```json
{
  "business_profile": "英科医疗业务边界……",
  "relevance_threshold": 70,
  "batch_size": 20,
  "content_max_chars": 30000,
  "auto_analyze": false,
  "auto_report": false
}
```

自动开关默认关闭。`auto_report=true` 只有在 `auto_analyze=true` 且当天存在已审核相关新闻时才会执行。

## 情报日报

### `POST /api/reports`

按新闻发布日期和分类生成后台日报任务：

```json
{
  "report_date": "2026-07-20",
  "categories": ["market_demand"]
}
```

`categories=[]` 表示全部分类。所选范围没有已通过相关性审核的新闻时返回 `422`。

### `GET /api/reports?limit=50`

返回已生成、运行中或失败的日报列表。

### `GET /api/reports/{report_id}`

返回日报结构化内容和全部依据文章，包括摘要、风险等级、关键进展、风险、机会、建议动作和监控清单。

## 错误约定

接口错误统一返回 FastAPI 的 `detail` 字段，例如：

```json
{
  "detail": "RSS 源名称或地址已存在"
}
```

常见状态码：`404` 资源不存在、`409` 配置冲突或任务正在运行、`422` 参数或业务范围校验失败、`503` DeepSeek Key 未配置、`500` 数据库错误。
