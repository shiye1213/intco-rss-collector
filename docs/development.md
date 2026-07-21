# 开发与运行指南

## 1. 环境要求

- Windows 10/11
- Python 3.12
- Git
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
Copy-Item .env.example .env
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

应用首次启动时自动创建 `data/rss_collector.db` 并初始化表结构及默认配置。该文件是本地运行数据，已被 Git 忽略。

清空本地数据前应先停止服务并备份数据库。不要在团队聊天、Issue 或提交记录中上传包含业务数据的数据库文件。

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
DEEPSEEK_API_KEY=你的API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=90
```

`.env` 已被 Git 忽略，不能提交真实密钥。模型可通过环境变量替换；默认使用支持结构化 JSON 输出的轻量模型。修改环境变量后必须重启应用。

### 搜索型 RSS

URL 模板必须包含 `{query}`。采集时系统将自动生成并 URL 编码查询表达式。

语言使用 BCP 47 风格代码，例如 `zh-CN`、`en-US`；国家/地区使用大写代码，例如 `CN`、`US`、`EU`。Google News 地址中的 `gl` 参数可以在国家字段留空时作为自动推断依据。

### 直连型 RSS

填写固定 RSS/Atom 地址。系统下载一次 Feed，再分别匹配所有启用关键词组。

### 关键词组

每行填写一个正文匹配词。查询表达式由系统生成，最终入库仍以标题或摘要是否命中匹配词为准。

## 8. 故障排查

- RSS 请求失败：检查网络、Feed 地址、HTTP 状态和对方访问限制。
- 读取到新闻但未入库：检查采集明细中的“时间窗外”和“命中”数量。
- 手动采集返回 409：已有采集任务正在运行，等待任务结束。
- 页面仍显示旧功能：确认服务已重启并刷新页面。
- 数据库被锁：确认没有同时启动多个应用进程或直接编辑 SQLite 文件。
- AI 按钮不可用：检查“系统设置”中的 DeepSeek 状态，并确认 `.env` 已填写 Key 后重启应用。
- 全文抓取失败：打开“AI 情报”的处理日志，检查原文 URL、HTTP 状态、正文长度和错误信息。登录墙、付费墙、JavaScript 动态渲染或强反爬页面不会使用 RSS 摘要兜底。
- Google News 原文解析失败：确认能访问 `news.google.com`；其原文解析依赖网页 RPC，协议变化后需更新 `GoogleNewsURLResolver`。
- 相关性审核失败：确认正文已成功抽取，再检查网络、模型名、Key 额度和第一次调用的 JSON 输出格式。
- 业务分析失败：文章仍保留真相关状态，可重新执行待办任务；检查第二次调用的 JSON 输出格式。
- 无法生成日报：确认所选日期和分类存在“业务分析成功”的新闻；日报按新闻发布时间而不是采集时间筛选。

## 9. 当前部署约束

只能使用单个 Uvicorn worker，因为调度器和采集互斥锁都在应用进程内。面向团队共享或生产部署前，需要增加认证、外部任务调度、持久化数据库、备份和监控方案。
