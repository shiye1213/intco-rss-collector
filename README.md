# 英科医疗 RSS 信息采集系统

面向市场与行业信息跟踪的 RSS 增量采集 MVP。系统按照可配置关键词从 Google News 等 RSS 源获取新闻，每日自动执行，也支持手动触发，并保存文章、增量游标及完整采集日志。

## 当前能力

- 管理搜索型和直连型 RSS 源。
- 管理关键词组，并根据匹配词自动生成 `("词1" OR "词2")` 查询表达式。
- 按“RSS 源 × 关键词组”维护独立增量游标。
- 第一次只采集当天内容，后续只采集上次成功时间至本次启动时间之间的内容。
- 按标题和摘要匹配关键词，并通过规范化 URL 与文章指纹去重。
- 去重后仍保留文章在全部 RSS 源中的来源链接、GUID、语言、国家和分类。
- 对发布方名称、时间、分类数组和来源元数据进行统一格式化。
- 支持每日定时采集、手动采集、文章检索和采集日志查看。

## 技术栈

- Python 3.12
- FastAPI、Pydantic、Uvicorn
- SQLite
- HTML、CSS、原生 JavaScript
- pytest

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
.\start.ps1
```

浏览器访问 `http://127.0.0.1:8000`。在 VS Code 中也可以按 `F5`，选择“启动 RSS 采集系统”。

运行测试：

```powershell
python -m pytest -q
```

## 文档

- [项目说明](docs/project-overview.md)
- [系统架构](docs/architecture.md)
- [API 说明](docs/api.md)
- [开发与运行](docs/development.md)
- [团队贡献规范](CONTRIBUTING.md)

FastAPI 启动后还可访问 `http://127.0.0.1:8000/docs` 查看自动生成的 OpenAPI 调试页面。

## 数据与安全

运行数据保存在 `data/rss_collector.db`。数据库、日志、虚拟环境和本地环境变量已经加入 `.gitignore`，不会提交到仓库。项目目前没有账号认证，部署到共享环境前必须增加身份认证和访问控制。

## 当前限制

- 当前调度器运行在单个 Web 进程内，不适合直接多进程部署。
- SQLite 适合当前 MVP 和小团队试用，数据量或并发增加后应迁移到 PostgreSQL。
- 当前没有历史日期范围补采、全文抓取、AI 内容分类和消息通知能力。
- 当前保存 RSS 提供的来源链接，尚未自动解析 Google News 跳转后的最终原文地址。
