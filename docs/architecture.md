# 系统架构

## 1. 总体结构

```mermaid
flowchart LR
    U["业务用户"] --> UI["HTML/CSS/JavaScript 管理页面"]
    UI -->|"REST API"| API["FastAPI 应用"]
    API --> DB["SQLite"]
    API --> CM["采集任务管理器"]
    SCH["每日调度器"] --> CM
    CM --> COL["RSS 采集器"]
    COL -->|"HTTP/HTTPS"| RSS["Google News 与其他 RSS 源"]
    COL --> PARSE["RSS/Atom XML 解析与过滤"]
    PARSE --> DB
```

当前采用单体架构，Web 接口、调度器和采集器运行在同一个 Python 进程中，适合本地开发和 MVP 验证。

## 2. 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| Web/API | `app/main.py` | FastAPI 生命周期、接口、参数校验、静态文件服务 |
| 采集器 | `app/collector.py` | Feed 下载、解析、匹配、时间过滤、去重和入库 |
| 数据访问 | `app/database.py` | SQLite 表结构、迁移和数据访问方法 |
| 调度器 | `app/scheduler.py` | 北京时间每日执行和下次执行时间计算 |
| 查询构建 | `app/query_builder.py` | 将匹配词转换为 Google News 查询表达式 |
| 前端 | `static/` | 文章、日志、RSS 源、关键词组和设置界面 |
| 测试 | `tests/` | 解析、时间窗、游标、失败和调度测试 |

## 3. 采集数据流

```mermaid
sequenceDiagram
    participant User as 用户或调度器
    participant API as FastAPI
    participant Manager as CollectionManager
    participant Collector as Collector
    participant Feed as RSS 源
    participant DB as SQLite

    User->>API: 触发采集
    API->>Manager: 创建运行记录并获取任务锁
    Manager->>Collector: 后台执行
    Collector->>DB: 读取启用源、关键词及游标
    Collector->>Feed: 请求 RSS/Atom
    Feed-->>Collector: XML Feed
    Collector->>Collector: 解析、时间过滤、关键词匹配、去重
    Collector->>DB: 保存文章、明细并推进成功游标
    Collector->>DB: 完成采集运行日志
```

## 4. 数据模型

| 表 | 用途 |
|---|---|
| `rss_sources` | RSS 源配置、类型、语言和启用状态 |
| `keywords` | 关键词组、查询表达式和正文匹配词 |
| `articles` | 新闻标题、链接、发布方、摘要和发布时间 |
| `article_sources` | 文章的全部 RSS 来源、链接、GUID、语言、国家、分类及发现时间 |
| `article_keywords` | 文章与命中关键词组的多对多关系 |
| `collection_cursors` | 每个源和关键词组合的上次成功时间 |
| `collection_runs` | 一次手动或定时采集的汇总日志 |
| `collection_run_details` | 每个源和关键词组合的采集明细 |
| `app_settings` | 调度时间、时区和上次调度日期 |

## 5. 关键设计

### 增量与失败恢复

游标只在对应任务成功后推进。某个 RSS 源请求或解析失败不会影响其他组合，也不会造成该组合采集范围丢失。

### 去重

系统先清理 URL 中的常见跟踪参数，再以规范化 URL 去重；同时计算“标题 + 发布方 + 发布日期”的 SHA-256 指纹，处理同一新闻使用不同跟踪链接的情况。

文章主记录去重后，每次命中的 RSS 来源仍写入 `article_sources`。因此同一文章来自多个 Feed 时只产生一篇文章，但不会丢失来源证据。发布方经过 Unicode NFKC、空白和边界符号规范化；时间统一保存为 UTC，分类统一保存为 JSON 数组。

### 并发控制

`CollectionManager` 使用进程内锁保证同一时间只有一个采集任务。当前设计要求使用单个 Uvicorn worker；多进程部署前需要改为数据库锁或分布式锁。

### 时区

业务时间使用 `Asia/Shanghai`，数据库时间统一保存为 UTC ISO 8601 字符串，展示时再转换为本地时间。

## 6. 生产化演进

建议将系统拆分为 API、任务队列 worker、调度服务和 PostgreSQL。通过 Redis/Celery、RQ 或同类成熟方案处理重试、并发和任务状态，并在 API 前增加身份认证、HTTPS、访问日志和监控告警。
