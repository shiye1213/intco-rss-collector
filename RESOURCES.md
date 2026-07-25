# 学习资源

本课程优先引用官方文档和项目自身源码。官方文档用于确认规则，课程页面负责把它们翻译成适合零基础、能直接用于本项目的解释。

## Knowledge：高可信资料

- [Python 官方教程](https://docs.python.org/3/tutorial/index.html)：语言基础、数据结构、函数、模块、异常和类。它假设读者已有一点编程经验，因此本课程会先做更直白的项目化讲解。
- [FastAPI 官方教程](https://fastapi.tiangolo.com/tutorial/)：路由、请求体、错误处理、静态文件和测试，是理解 `app/main.py` 的主要依据。
- [Pydantic 官方文档](https://docs.pydantic.dev/latest/)：理解类型标注如何变成请求校验与数据模型。
- [SQLite Quickstart](https://www.sqlite.org/quickstart.html)：理解本地数据库、表和 SQL 查询。
- [MDN HTTP 概览](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Overview)：浏览器与后端如何通过请求和响应交流。
- [RSS 2.0 规范](https://www.rssboard.org/rss-specification)：RSS channel、item、link、title 等字段的权威定义。
- [pytest 入门](https://docs.pytest.org/en/stable/getting-started.html)：如何运行和编写项目测试。
- [OpenAI Web Search 指南](https://developers.openai.com/api/docs/guides/tools-web-search)：项目中 GPT 网页读取能力所对应的官方工具说明。
- [项目 README](README.md)：本项目的启动方式、功能说明和配置入口。
- [项目技术设计](docs/technical-design.md)：本项目架构、数据模型和关键技术决策的本地说明。

## Wisdom：遇到真实问题时去哪里讨论

- [Python Discourse](https://discuss.python.org/)：Python 语言、打包和生态的官方社区。
- [FastAPI Discussions](https://github.com/fastapi/fastapi/discussions)：FastAPI 使用问题与框架设计讨论。
- [SQLite Forum](https://sqlite.org/forum/forum)：SQLite 行为、SQL 和性能问题的官方论坛。

社区回答可能过时或只适用于特定环境。使用时先确认版本、复现条件，再回到官方文档或测试验证。

## 当前资料缺口

- `课题7.docx` 是项目需求背景，但它位于项目目录之外；课程涉及需求验收时再单独建立需求对照表。
- CCTQ 是兼容接口服务，具体支持能力可能与 OpenAI 官方接口不同；相关课程将以项目中的实际响应和自动化测试为准。
