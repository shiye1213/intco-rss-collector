# 英科医疗 RSS 信息采集系统

面向市场与行业信息跟踪的增量采集与 AI 情报分析系统。系统优先从 Google News 等 RSS 源获取候选新闻，对没有 RSS 的站点使用同站网页爬虫兜底；随后由 CCTQ 转发的 GPT-5.4 mini 通过 OpenAI Responses API 兼容接口和内置网页搜索直接读取并保存正文，再由 DeepSeek 分阶段完成业务相关性审核与情报分析，最后按新闻发布日期生成可追溯的综合日报。

## 当前能力

- 管理搜索型 RSS、直连 RSS 和网页爬虫数据源；RSS 地址返回 HTML 时自动切换爬虫。
- 网页爬虫从新闻列表页提取同站文章，单次最多读取 30 篇，并从 JSON-LD、Open Graph、`time` 元素或日期 URL 中识别发布日期。
- 管理关键词检索策略，按“主题词 × 业务信号词 − 排除词 + 回溯窗口”生成 Google News 查询表达式；分类默认同时提供保精度的核心组和补同义表达的拓展召回组，运行时依据 RSS 源语言自动拆分中英文词。
- 按“数据源 × 关键词组”维护独立增量游标。
- 新关键词第一次按其回溯天数采集，后续只采集上次成功时间至本次启动时间之间的内容；长时间失败时搜索回溯窗口会自动扩展。
- 拓展召回组可强制标题或摘要命中主题词，过滤搜索引擎误召回后再进入 AI 审核；核心组可保留搜索引擎语义召回能力，并通过规范化 URL 与文章指纹去重。
- 去重后仍保留文章在全部 RSS 源中的来源链接、GUID、语言、国家和分类。
- 对发布方名称、时间、分类数组和来源元数据进行统一格式化。
- 支持每日定时采集、手动采集、文章检索和采集日志查看。
- 将候选链接、标题和发布方交给 CCTQ 的 `gpt-5.4-mini`，强制调用内置 `web_search` 联网读取正文；项目本身不下载或解析新闻 HTML。
- 只有响应包含实际网页搜索结果、模型明确确认成功且正文达到最低长度时才保存，避免按标题或链接编造正文。
- OpenAI 网页搜索调用失败或返回 HTTP 429 时立即暂停当前队列，未开始文章保留待办。
- 第一次 DeepSeek 调用依据正文判断真实相关性、主/次业务分类并保存审核证据。
- 只有通过阈值的真相关文章才写入业务新闻表并进行第二次分析。
- 第二次 DeepSeek 调用生成摘要、固定分类、影响方向、风险因素和建议动作。
- 按新闻发布日期生成综合风险日报；各业务方面、风险、机会、建议和监控项均附发布方与原文链接。
- 保存 AI 批次日志、模型、Prompt 版本、Token 用量和原始响应，支持审计。
- 手动分析一次点击即可处理当时全部待办，并按配置的批次大小连续执行；也可配置为采集后自动分析，并为当天生成一份综合日报。
- 全文读取失败按 OpenAI 网络、限流、未联网、网页不可用、正文不完整和响应格式分类；瞬时错误最多自动尝试 3 次并指数退避。
- AI 情报页面提供失败列表、错误原因、立即重试和忽略入口。
- 系统设置提供错误数据、指定日期前历史数据和全部采集数据三层清理；执行前自动备份 MySQL，任务运行中禁止清理。
- 系统设置可查看和修改企业业务边界、相关性审核和日报提示词；固定 JSON、安全、业务分类和来源引用约束由系统追加。
- 默认包含马来西亚通用 Google News，以及 The Star、The Edge 站点限定搜索源。

## 技术栈

- Python 3.12
- FastAPI、Pydantic、Uvicorn
- MySQL 8.4、PyMySQL
- HTML、CSS、原生 JavaScript
- pytest

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# 先启动 MySQL 8.4（已安装 Docker 时）
docker compose up -d mysql
# 编辑 .env，确认 DATABASE_URL，并填写 OPENAI_API_KEY、DEEPSEEK_API_KEY 和可选的飞书 Webhook 配置
.\start.ps1
```

使用 Windows 本机安装的 MySQL 时，首次启动前执行一键建库建表脚本：

```powershell
python scripts/setup_mysql.py
```

脚本读取 `.env` 中的 `DATABASE_URL`，提示输入 MySQL root 密码，随后创建数据库、应用用户、18 张业务表和默认配置；不会删除已有表数据，也不会保存 root 密码。
浏览器访问 `http://127.0.0.1:8000`。在 VS Code 中也可以按 `F5`，选择“启动 RSS 情报系统”。

如需把现有 SQLite 数据导入 MySQL，请先确认目标 MySQL 可连接，再执行：

```powershell
python scripts/migrate_sqlite_to_mysql.py --confirm REPLACE_MYSQL
```

该命令会先备份并清空目标 MySQL，再完整导入 `data/rss_collector.db`；源 SQLite 文件保持不变。

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

运行数据保存在 `DATABASE_URL` 指定的 MySQL 数据库中，页面清理前的逻辑备份保存在 `data/backups/`。原有 `data/rss_collector.db` 不会被删除，可用于迁移或回退。OpenAI、DeepSeek 与飞书 Webhook 配置只从 `.env` 或进程环境变量读取。数据库、备份、日志、虚拟环境和本地环境变量已经加入 `.gitignore`，不会提交到仓库。项目目前没有账号认证，部署到共享环境前必须增加身份认证和访问控制。

## 当前限制

- 当前调度器运行在单个 Web 进程内，不适合直接多进程部署。
- 默认使用单个 MySQL 实例；任务调度和互斥状态仍由单个 Web 进程维护。
- OpenAI 内置网页搜索无法访问、只返回摘要或不能确认完整正文时，任务记录全文读取失败，不会用 RSS 摘要替代。
- 当前没有历史日期范围补采和消息通知能力。
- AI 调用产生外部 API 成本，自动分析和自动日报默认关闭。
- OpenAI 内置网页搜索除模型 Token 外还按工具调用计费，可访问范围由 OpenAI 网页搜索服务决定。
