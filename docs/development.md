# 开发与运行指南

## 1. 环境要求

- Windows 10/11
- Python 3.12
- Git
- MySQL 8.4（或 Docker Desktop）
- VS Code 与 Python 扩展
- 能访问配置的 RSS 源

## 2. 初始化

```powershell
git clone <repository-url>
Set-Location rss_collector
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
# 编辑项目根目录的 .env，填入所需的本地环境变量
```

如果 PowerShell 禁止激活脚本，可以直接使用 `.\.venv\Scripts\python.exe` 执行后续命令。

## 3. 启动

```powershell
.\start.ps1
```

也可以直接运行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

应用地址：`http://127.0.0.1:8000`

## 4. 测试

```powershell
python -m pytest -q
```

提交代码前必须确保测试通过。涉及采集规则、时间窗、游标、去重或调度逻辑的修改需要同步增加测试。

## 5. 数据库

默认数据库为 MySQL 8.4。首次开发可使用仓库内的 Compose 配置：

```powershell
docker compose up -d mysql
docker compose ps
```

应用读取 `.env` 中的 `DATABASE_URL`，启动时自动初始化表结构和默认配置：

```dotenv
DATABASE_URL=mysql://rss_collector:rss_collector@127.0.0.1:3306/rss_collector?charset=utf8mb4
```

如果使用 Windows 本机安装的 MySQL，首次运行：

```powershell
python scripts/setup_mysql.py
```

脚本只在终端中读取 MySQL root 密码，自动创建数据库、修复 `DATABASE_URL` 对应用户的密码和权限，并初始化全部表与默认配置。该过程不会删除已有数据，可重复执行。
优先使用“系统设置 → 数据清理”：它会先预览影响，在确认后将 MySQL 逻辑备份写入 `data/backups/`，并在采集、AI 分析或日报运行时拒绝执行。原有 `data/rss_collector.db` 不会自动删除；迁移确认前请保留。不要在团队聊天、Issue 或提交记录中上传包含业务数据的数据库或备份文件。

迁移现有 SQLite 数据：

```powershell
python scripts/migrate_sqlite_to_mysql.py --confirm REPLACE_MYSQL
```

迁移工具会先为目标 MySQL 生成 `.sql` 备份，再清空目标表并保留原主键导入；源 SQLite 以只读方式打开，不会删除或改写。

## 6. 调试

VS Code 已配置仓库内 `.venv` 解释器和 `.vscode/launch.json`。按 `F5` 选择“启动 RSS 情报系统”即可启用自动重载和断点调试。

常用检查：

```powershell
python -m compileall app tests
python -m pytest -q
```

前端使用原生 JavaScript，无需单独构建。修改 `static/` 后刷新浏览器即可。

## 7. 配置说明

### DeepSeek

在项目根目录 `.env` 中配置：

```dotenv
DATABASE_URL=mysql://rss_collector:rss_collector@127.0.0.1:3306/rss_collector?charset=utf8mb4
DEEPSEEK_API_KEY=你的API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=90
OPENAI_API_KEY=
OPENAI_BASE_URL=https://www.cctq.ai/v1
OPENAI_WEB_MODEL=gpt-5.4-mini
OPENAI_WEB_TIMEOUT=180
OPENAI_WEB_MAX_OUTPUT_TOKENS=32000
```

`.env` 已被 Git 忽略，不能提交真实密钥。模型可通过环境变量替换；默认使用支持结构化 JSON 输出的轻量模型。修改环境变量后必须重启应用。

### 搜索型 RSS

URL 模板必须包含 `{query}`。采集时系统将自动生成并 URL 编码查询表达式。

语言使用 BCP 47 风格代码，例如 `zh-CN`、`en-US`；国家/地区使用大写代码，例如 `CN`、`US`、`EU`。Google News 地址中的 `gl` 参数可以在国家字段留空时作为自动推断依据。

### 直连型 RSS

填写固定 RSS/Atom 地址。系统下载一次 Feed，再分别匹配所有启用关键词组。

### 网页爬虫

网页爬虫总开关默认关闭；需要使用时，先在“系统设置 → 采集调度”中开启“启用网页爬虫”。站点没有 RSS/Atom 时，选择“网页爬虫（无 RSS）”并填写固定的新闻列表页地址。采集器会在列表页中筛选同站文章链接，单次最多读取 30 篇详情页，再从 JSON-LD、Open Graph、HTML `time` 元素或日期型 URL 中提取标题、摘要、发布方和发布日期。只有能识别发布日期的文章才会进入既有时间窗、关键词匹配和去重流程。

总开关开启时，直连型或搜索型 RSS 地址意外返回 HTML 才会进入同一兜底流程。通用爬虫不执行 JavaScript，也不会跨站或递归扫描整个网站。默认遵守 `robots.txt`、同域请求间隔 3 秒；遇到 403、429、5xx 或验证码时会记录原因并冷却来源。系统设置可调整间隔与默认冷却时间，但生产环境不建议关闭 robots.txt。强登录、验证码或纯前端渲染页面应优先寻找官方 Feed 或授权 API，不应尝试绕过。

### 关键词组

主题词填写产品、企业或关注对象。业务信号词用于要求 Google News 同时命中关税、法规、价格、需求、产能、采购、召回、供应链等情报语境；排除词用于过滤拳击、橄榄球、棒球等歧义；回溯天数同时控制 Google News 搜索范围和新关键词第一次入库的时间窗口。查询表达式由后端统一生成，前端只调用预览接口，不自行拼接。运行时会按 RSS 源的语言拆分中英文主题词、业务信号词和排除词；混合词组无需手工复制，无对应语言主题词的来源组合会被跳过。Google News 对超长复合查询处理不稳定，因此后端将表达式限制为 200 字符，超过时应拆成多个更聚焦的关键词组。

建议每个关键词分类同时维护核心组和拓展召回组：核心组使用产品专名保证命中率；拓展组补充“橡胶手套、手套行业、glove sector、glove makers”等行业写法，并开启“标题/摘要必须命中主题词”。这样搜索引擎返回的候选必须先通过本地主题校验，再进入 AI 业务相关性和专项分类审核。关键词策略发生变化时系统会清除该组旧的派生命中和游标，下一次按完整回溯窗口重新验证。

默认启用的经营情报词还覆盖英科医疗自身、手套供需与价格、主要竞争对手、关键原材料与物流、康复护理、理疗护理和公共卫生需求。没有产品或原材料约束的通用关税、美国法律工具和贸易救济词默认关闭；需要宏观研究时再临时开启，避免大量无关候选消耗全文读取和 AI 审核额度。

## 8. 故障排查

- 数据源请求失败：检查网络、Feed/新闻列表页地址、HTTP 状态和对方访问限制。
- 网页爬虫未提取到文章：确认列表页是服务端渲染、文章链接与列表页同站，且详情页包含结构化发布日期、`time` 元素或日期型 URL。
- 爬虫来源提示“总开关已关闭”：在“系统设置 → 采集调度”中开启“启用网页爬虫”；该开关默认关闭，普通 RSS 采集不受影响。
- 数据源显示“冷却中”：查看反爬类型、错误和恢复时间。429 按站点 `Retry-After` 自动恢复；robots.txt 禁止、403 或验证码应改用官方 RSS/API，而不是反复重试。
- 读取到新闻但未入库：检查采集明细中的“时间窗外”和“命中”数量。
- 手动采集返回 409：已有采集任务正在运行，等待任务结束。
- 页面仍显示旧功能：确认服务已重启并刷新页面。
- 无法连接数据库：确认 MySQL 已启动，且 `.env` 中的主机、端口、用户名、密码和数据库名正确。
- AI 按钮不可用：检查“系统设置”中的 DeepSeek 状态，并确认 `.env` 已填写 Key 后重启应用。
- 全文读取失败：打开“AI 情报”的失败列表，查看错误分类、次数和下一次重试时间；可立即重试或忽略。正文由 CCTQ 的 GPT-5.4 mini 内置网页搜索读取，登录墙、付费墙或搜索服务不可达时不会使用 RSS 摘要兜底。
- OpenAI 网页读取限流或搜索服务不可用：当前队列会在首次 HTTP 429 或搜索调用失败后暂停，尚未开始的文章保留为待办；服务恢复后再点击处理即可。
- 相关性审核失败：确认正文已成功抽取，再检查网络、模型名、Key 额度和第一次调用的 JSON 输出格式。
- 业务分析失败：文章仍保留真相关状态，可重新执行待办任务；检查第二次调用的 JSON 输出格式。
- 无法生成日报：确认所选日期存在“业务分析成功”的新闻；日报按新闻发布时间而不是采集时间筛选，并汇总当天全部合格新闻。

## 9. 当前部署约束

只能使用单个 Uvicorn worker，因为调度器和采集互斥锁都在应用进程内。面向团队共享或生产部署前，需要增加认证、外部任务调度、持久化数据库、备份和监控方案。
