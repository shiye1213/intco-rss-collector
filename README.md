# 英科医疗 RSS 信息采集系统

面向市场与行业信息跟踪的 RSS 增量采集与 AI 情报分析系统。系统从 Google News 等 RSS 源获取候选新闻，解析出版社原文地址并抽取网页正文，再通过 DeepSeek 分阶段完成业务相关性审核与情报分析，最后按日期和分类生成可追溯的日报。

## 当前能力

- 管理搜索型和直连型 RSS 源。
- 管理关键词组，并根据匹配词自动生成 `("词1" OR "词2")` 查询表达式。
- 按“RSS 源 × 关键词组”维护独立增量游标。
- 第一次只采集当天内容，后续只采集上次成功时间至本次启动时间之间的内容。
- 按标题和摘要匹配关键词，并通过规范化 URL 与文章指纹去重。
- 去重后仍保留文章在全部 RSS 源中的来源链接、GUID、语言、国家和分类。
- 对发布方名称、时间、分类数组和来源元数据进行统一格式化。
- 支持每日定时采集、手动采集、文章检索和采集日志查看。
- 解析 Google News 聚合链接并使用 Trafilatura 抽取、清洗出版社网页正文。
- 第一次 DeepSeek 调用只依据正文判断真实相关性并保存审核证据。
- 只有通过阈值的真相关文章才写入业务新闻表并进行第二次分析。
- 第二次 DeepSeek 调用生成摘要、固定分类、影响方向、风险因素和建议动作。
- 按新闻发布日期与分类生成风险日报并持久化保存。
- 保存 AI 批次日志、模型、Prompt 版本、Token 用量和原始响应，支持审计。
- 支持手动分析，也可配置为采集后自动分析和自动生成当天日报。

## 技术栈

- Python 3.12
- FastAPI、Pydantic、Uvicorn、Trafilatura
- SQLite
- HTML、CSS、原生 JavaScript
- pytest

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# 编辑 .env，填写 DEEPSEEK_API_KEY
.\start.ps1
```

浏览器访问 `http://127.0.0.1:8000`。在 VS Code 中也可以按 `F5`，选择“启动 RSS 情报系统”。

运行测试：

```powershell
python -m pytest -q
```

## 文档

- [详细技术设计说明](docs/technical-design.md)
- [项目说明](docs/project-overview.md)
- [系统架构](docs/architecture.md)
- [API 说明](docs/api.md)
- [AI Prompt 与风险规则](docs/ai-prompts.md)
- [开发与运行](docs/development.md)
- [团队贡献规范](CONTRIBUTING.md)

FastAPI 启动后还可访问 `http://127.0.0.1:8000/docs` 查看自动生成的 OpenAPI 调试页面。

## 数据与安全

运行数据保存在 `data/rss_collector.db`。DeepSeek Key 只从 `.env` 或进程环境变量读取。数据库、日志、虚拟环境和本地环境变量已经加入 `.gitignore`，不会提交到仓库。项目目前没有账号认证，部署到共享环境前必须增加身份认证和访问控制。

## 当前限制

- 当前调度器运行在单个 Web 进程内，不适合直接多进程部署。
- SQLite 适合当前 MVP 和小团队试用，数据量或并发增加后应迁移到 PostgreSQL。
- JavaScript 动态渲染、登录墙、付费墙或强反爬页面可能无法抽取正文；此时任务记录全文失败，不会用 RSS 摘要替代。
- 当前没有历史日期范围补采和消息通知能力。
- AI 调用产生外部 API 成本，自动分析和自动日报默认关闭。
- Google News 原文链接解析依赖其非公开网页 RPC，若 Google 调整协议需要同步维护解析器。
