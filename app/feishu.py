from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_WEBHOOK_BODY_BYTES = 20 * 1024
WebhookOpener = Callable[[Request, float], bytes]


class FeishuWebhookError(RuntimeError):
    """Raised when a Feishu custom bot rejects or cannot receive a message."""


class FeishuConfigurationError(FeishuWebhookError):
    """Raised when the Feishu webhook configuration is missing or invalid."""


def build_feishu_signature(timestamp: int, secret: str) -> str:
    """Build the signature required by a signed Feishu custom bot webhook."""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _default_opener(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class FeishuWebhookClient:
    def __init__(
        self,
        webhook_url: str = "",
        secret: str = "",
        *,
        timeout: float = 15,
        opener: WebhookOpener | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.secret = secret.strip()
        self.timeout = timeout
        self._opener = opener or _default_opener
        self._clock = clock

    @classmethod
    def from_env(cls) -> FeishuWebhookClient:
        raw_timeout = os.getenv("FEISHU_WEBHOOK_TIMEOUT", "15")
        try:
            timeout = max(1.0, min(60.0, float(raw_timeout)))
        except ValueError:
            timeout = 15.0
        return cls(
            os.getenv("FEISHU_WEBHOOK_URL", ""),
            os.getenv("FEISHU_WEBHOOK_SECRET", ""),
            timeout=timeout,
        )

    @property
    def configured(self) -> bool:
        try:
            self._validate_configuration()
        except FeishuConfigurationError:
            return False
        return True

    def _validate_configuration(self) -> None:
        if not self.webhook_url:
            raise FeishuConfigurationError("尚未配置 FEISHU_WEBHOOK_URL")
        parsed = urlsplit(self.webhook_url)
        path_prefix = "/open-apis/bot/v2/hook/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "open.feishu.cn"
            or not parsed.path.startswith(path_prefix)
            or parsed.path == path_prefix
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise FeishuConfigurationError(
                "FEISHU_WEBHOOK_URL 必须是飞书群自定义机器人的 HTTPS Webhook 地址"
            )

    def send_card(self, card: dict[str, Any]) -> dict[str, Any]:
        self._validate_configuration()
        body: dict[str, Any] = {"msg_type": "interactive", "card": card}
        if self.secret:
            timestamp = int(self._clock())
            body.update(
                {
                    "timestamp": str(timestamp),
                    "sign": build_feishu_signature(timestamp, self.secret),
                }
            )
        payload = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > MAX_WEBHOOK_BODY_BYTES:
            raise FeishuWebhookError(
                f"飞书消息体为 {len(payload)} 字节，超过 20 KB 限制"
            )
        request = Request(
            self.webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "rss-collector-feishu-webhook/1.0",
            },
            method="POST",
        )
        try:
            raw_response = self._opener(request, self.timeout)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise FeishuWebhookError(
                f"飞书 Webhook HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FeishuWebhookError(f"无法连接飞书 Webhook: {exc}") from exc

        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuWebhookError("飞书 Webhook 返回了无效 JSON") from exc
        if not isinstance(response, dict) or response.get("code") != 0:
            code = response.get("code", "unknown") if isinstance(response, dict) else "unknown"
            message = (
                response.get("msg", "未知错误")
                if isinstance(response, dict)
                else "返回内容不是 JSON 对象"
            )
            raise FeishuWebhookError(f"飞书 Webhook 拒绝消息（{code}）：{message}")
        return response


def _truncate(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: max(0, limit - 1)]}…"


def _markdown_text(value: Any, limit: int) -> str:
    return (
        _truncate(value, limit)
        .replace("\\", "\\\\")
        .replace("[", "【")
        .replace("]", "】")
    )


def _source_link(source: dict[str, Any], index: int) -> str:
    url = str(source.get("source_url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    safe_url = url.replace("(", "%28").replace(")", "%29")
    publisher = _markdown_text(source.get("publisher") or f"来源{index}", 28)
    title = _markdown_text(source.get("title") or "", 42)
    label = f"{publisher} · {title}" if title else publisher
    return f"[{label}]({safe_url})"


def _item_sources(item: dict[str, Any], *, maximum: int = 2) -> str:
    links = [
        link
        for index, source in enumerate(item.get("sources") or [], start=1)
        if (link := _source_link(source, index))
    ][:maximum]
    return "；".join(links)


def _section(
    title: str,
    items: list[dict[str, Any]],
    *,
    content_key: str = "content",
    maximum: int = 3,
) -> list[str]:
    if not items:
        return []
    lines = [f"**{title}**"]
    for index, item in enumerate(items[:maximum], start=1):
        content = _markdown_text(item.get(content_key), 220)
        sources = _item_sources(item)
        suffix = f"\n   出处：{sources}" if sources else ""
        lines.append(f"{index}. {content}{suffix}")
    return lines


def build_daily_report_card(report: dict[str, Any]) -> dict[str, Any]:
    category = _markdown_text(
        report.get("keyword_category_name") or "综合情报", 50
    )
    risk_level = str(report.get("risk_level") or "low")
    risk_labels = {
        "low": "低",
        "medium": "中",
        "high": "高",
        "critical": "严重",
    }
    header_templates = {
        "low": "green",
        "medium": "orange",
        "high": "red",
        "critical": "red",
    }
    title = _markdown_text(
        report.get("title") or f"{category}日报", 120
    )
    lines = [
        f"**日报日期：** {_markdown_text(report.get('report_date'), 20)}",
        f"**关键词分类：** {category}",
        (
            f"**风险等级：** {risk_labels.get(risk_level, risk_level)}"
            f"（{int(report.get('risk_score') or 0)}/100）"
        ),
        f"**文章来源：** {int(report.get('article_count') or 0)} 篇",
        "",
        "**管理层摘要**",
        _markdown_text(report.get("executive_summary"), 700),
    ]
    risk_basis = _markdown_text(report.get("risk_basis"), 360)
    if risk_basis:
        lines.extend(["", f"**风险依据：** {risk_basis}"])

    developments = report.get("key_developments") or []
    if developments:
        lines.extend(["", "**关键进展**"])
        for index, item in enumerate(developments[:3], start=1):
            heading = _markdown_text(item.get("title"), 100)
            finding = _markdown_text(item.get("finding"), 220)
            impact = _markdown_text(item.get("business_impact"), 180)
            sources = _item_sources(item, maximum=1)
            lines.append(f"{index}. **{heading}**：{finding}")
            if impact:
                lines.append(f"   业务影响：{impact}")
            if sources:
                lines.append(f"   出处：{sources}")

    for section_title, field in (
        ("关键风险", "key_risks"),
        ("业务机会", "opportunities"),
        ("建议动作", "recommended_actions"),
        ("后续监控", "watchlist"),
    ):
        section_lines = _section(section_title, report.get(field) or [])
        if section_lines:
            lines.extend(["", *section_lines])

    source_links = [
        link
        for index, source in enumerate(report.get("sources") or [], start=1)
        if (link := _source_link(source, index))
    ][:5]
    if source_links:
        lines.extend(["", "**全部来源（部分）**", "\n".join(source_links)])

    return {
        "schema": "2.0",
        "header": {
            "template": header_templates.get(risk_level, "blue"),
            "title": {
                "tag": "plain_text",
                "content": f"{category}日报｜{title}",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "\n".join(lines),
                }
            ]
        },
    }


class FeishuReportPublisher:
    def __init__(self, repository: Any, client: FeishuWebhookClient) -> None:
        self.repository = repository
        self.client = client

    @property
    def configured(self) -> bool:
        return self.client.configured

    def push(self, report_id: int) -> dict[str, Any]:
        report = self.repository.get_report(report_id)
        if report is None:
            raise ValueError("日报不存在")
        if report.get("status") != "success":
            raise ValueError("只有生成成功的日报才能推送")
        if report.get("feishu_status") == "sending":
            raise ValueError("该日报正在推送飞书")

        self.repository.update_report_feishu_status(report_id, "sending")
        try:
            response = self.client.send_card(build_daily_report_card(report))
        except Exception as exc:
            self.repository.update_report_feishu_status(
                report_id, "failed", str(exc)
            )
            raise
        self.repository.update_report_feishu_status(report_id, "success")
        return response
