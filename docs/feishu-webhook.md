# 飞书群自定义机器人 Webhook 接入调研

调研日期：2026-07-28

## 结论

本项目的“分类日报定时推送到固定群”适合使用**群自定义机器人 Webhook**。它不需要创建飞书应用或获取 `tenant_access_token`，但一个自定义机器人只能向创建它的当前群聊单向推送，不能读取群数据、响应用户消息或执行群管理。[自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)；[自定义机器人发送卡片](https://open.feishu.cn/document/feishu-cards/quick-start/send-message-cards-with-custom-bot?lang=zh-CN)

用户给出的[“发送消息内容结构”](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)属于 **IM v1 应用机器人 OpenAPI**。该页明确声明“不适用于自定义机器人”；其中 `content` 是需要再次 JSON 序列化的字符串，而自定义机器人 Webhook 的 `content` 是 JSON 对象，不能照搬其最外层请求结构。

## Webhook 基本协议

- 地址格式：`https://open.feishu.cn/open-apis/bot/v2/hook/<token>`。
- 方法：`POST`。
- 请求头：`Content-Type: application/json; charset=utf-8`。官方 Webhook 示例写作 `application/json`，机器人 FAQ 建议显式声明 UTF-8，避免中文乱码。
- 不传 `Authorization`、`receive_id` 或 `chat_id`；Webhook 本身已经绑定目标群。
- 成功以响应 JSON 的 `code == 0` 判断。`StatusCode` 和 `StatusMessage` 是兼容旧逻辑的冗余字段，不建议使用。
- 请求体格式错误示例为 `{"code":9499,"msg":"Bad Request","data":{}}`。
- 应使用带 `/v2/` 的新版地址。旧 V1 地址为 `/open-apis/bot/hook/<token>`，仅支持配置 `title` 和 `text` 的纯文本消息；官方推荐使用 V2。

来源：[自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)；[消息常见问题](https://open.feishu.cn/document/server-docs/im-v1/faq)；[机器人常见问题](https://open.feishu.cn/document/faq/bot?lang=zh-CN)

最小文本请求：

```json
{
  "msg_type": "text",
  "content": {
    "text": "2026-07-28 贸易政策日报已生成"
  }
}
```

## 日报推荐消息格式

### 可选：`post` 富文本

`post` 支持标题、分段文本和可点击超链接，适合把每篇文章的“标题 + 摘要 + 来源”组成一个段落。自定义机器人文档明确支持 `text`、`a`、`at` 和 `img` 标签；其中 `a.href` 必须是合法 URL。[自定义机器人使用指南：发送富文本消息](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

```json
{
  "msg_type": "post",
  "content": {
    "post": {
      "zh_cn": {
        "title": "2026-07-28｜贸易政策日报",
        "content": [
          [
            {
              "tag": "text",
              "text": "1. 欧盟发布医疗器械贸易新规\n"
            },
            {
              "tag": "text",
              "text": "影响：可能增加出口合规成本。\n"
            },
            {
              "tag": "a",
              "text": "来源：欧盟委员会",
              "href": "https://example.com/article"
            }
          ]
        ]
      }
    }
  }
}
```

不建议直接把日报 Markdown 放进 `text` 消息：IM v1 文档所述的普通文本 Markdown 超链接能力明确不支持自定义机器人。Webhook 富文本的 `a` 标签是官方明确支持、兼容性更稳妥的来源链接方案。[IM v1 发送消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json)；[自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

### 当前实现：`interactive` 飞书卡片

卡片适合更清晰的标题、Markdown 正文和“查看原文”按钮。Webhook 请求使用 `msg_type: "interactive"`，并把卡片放在顶层 `card` 字段，而不是 IM v1 的字符串 `content`。但自定义机器人卡片只能通过按钮或文字链跳转 URL，不支持点击后回调服务端；也不能更新已发送的静态卡片。[自定义机器人使用指南：发送飞书卡片](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)；[使用自定义机器人发送飞书卡片](https://open.feishu.cn/document/feishu-cards/quick-start/send-message-cards-with-custom-bot?lang=zh-CN)

本项目当前使用 `interactive` 卡片：标题突出关键词分类和风险等级，正文展示管理层摘要、关键进展、风险、机会、建议动作、监控项及可点击来源链接。自定义机器人卡片保持单向、静态，不使用交互回调。`post` 仍可作为需要最简消息结构时的备选。

## 安全设置与签名

官方强烈建议启用安全设置；同一个机器人可同时使用关键词、IP 白名单、签名校验中的一种或多种。[自定义机器人使用指南：安全设置](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

推荐至少启用**签名校验**：

1. 使用当前 Unix 时间戳（秒）作为 `timestamp`。
2. 拼接 `timestamp + "\n" + secret` 作为 HMAC-SHA256 的 key。
3. 对空字节串计算 HMAC-SHA256，再进行 Base64 编码得到 `sign`。
4. 将字符串形式的 `timestamp` 和 `sign` 放进请求体顶层。

```python
import base64
import hashlib
import hmac
import time


def feishu_sign(secret: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return timestamp, base64.b64encode(digest).decode("utf-8")
```

签名时间戳与飞书当前时间相差不能超过 1 小时（3600 秒）；签名不匹配或过期返回 `code: 19021`。因此服务器时钟应保持同步。[自定义机器人使用指南：签名校验](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

其他安全方式：

- 关键词：最多 10 个，只要消息的文本字段包含任一关键词即可；失败返回 `19024`。关键词只检查 `text`、`title` 等文本值，不检查链接 `href`。若启用，建议固定使用“日报”作为关键词，并确保所有分类消息标题都包含它。
- IP 白名单：最多 10 个 IP 或网段，支持 `123.12.1.*`、`123.1.1.1/24` 形式；失败返回 `19022`。

来源：[自定义机器人使用指南：关键词、IP 白名单](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

Webhook URL 和签名密钥都应作为凭据管理：不要提交到 Git、写入日报内容或完整记录在日志中。官方特别提醒不要将 Webhook 地址公开在 GitLab、博客等公开位置。[自定义机器人使用指南](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

## 限制、错误和重试

- 单租户、单机器人：`100 次/分钟`、`5 次/秒`。
- 单次请求体：不得超过 `20 KB`。
- 官方建议避开 `10:00`、`17:30` 等整点或半点高峰，否则可能因系统压力出现 `11232` 限流错误。
- 格式错误：`9499`；签名失败或过期：`19021`；IP 不允许：`19022`；关键词未命中：`19024`。

来源：[自定义机器人使用指南：注意事项与错误示例](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)

官方文档在大小表述上存在不一致：[消息常见问题](https://open.feishu.cn/document/server-docs/im-v1/faq)写的是“建议 JSON 不超过 30 KB”，而自定义机器人主指南明确写的是“请求体不能超过 20 KB”。实现应采用更严格的 **20 KB** 上限，并按 UTF-8 序列化后的实际字节数检查。

实现上应同时检查 HTTP 状态码和 JSON `code`，仅 `code == 0` 视为成功。对网络错误、HTTP 5xx 和 `11232` 做有限次数的指数退避重试；`9499`、`19021`、`19022`、`19024` 属于配置或请求问题，不应盲目重试。后两句是基于官方错误语义的工程建议，不是官方承诺的重试规则。

## 对本项目的接入建议

1. 配置层增加全局自动推送开关；Webhook URL、可选签名密钥只从环境变量读取。若未来需要不同分类推送到不同群，再增加“分类 → Webhook 配置”映射。
2. 日报成功落库后再创建推送任务，使用已保存的日报及文章来源 URL 渲染消息，避免“生成成功但展示内容与推送内容不一致”。
3. 每个关键词分类单独推送，例如“贸易政策日报”“关税调整日报”“行业法规日报”，不要把分类混在同一份日报内。
4. 渲染前按 UTF-8 JSON 序列化后的**字节数**检查 20 KB 上限；当前卡片主动限制各区块条目数和文字长度，超限时拒绝发送并记录明确错误。
5. 保存报告的推送状态、成功时间和脱敏错误，支持页面查看状态及手动重推。
6. 自动推送与手动推送共用同一发送服务，均满足 Webhook 的关键词、IP 和签名规则。

## 待产品确认

- 所有分类发到同一个群，还是每个分类映射不同群？
- 推送采用生成后立即发送，还是每日固定时间批量发送？
- 是否需要将当前 `interactive` 卡片改为 `post` 富文本？
- 是否需要 @ 所有人？官方支持 `<at user_id="all">所有人</at>`，但群本身必须允许 @ 所有人；外部群 @ 单人只支持 Open ID。[自定义机器人使用指南：@ 用法](https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN?lang=zh-CN)
