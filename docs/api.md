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

## 错误约定

接口错误统一返回 FastAPI 的 `detail` 字段，例如：

```json
{
  "detail": "RSS 源名称或地址已存在"
}
```

常见状态码：`404` 资源不存在、`409` 配置冲突或采集任务正在运行、`422` 参数校验失败、`500` 数据库错误。
