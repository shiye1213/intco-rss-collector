from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FeishuWebhookError(RuntimeError):
    """The Feishu custom bot did not accept a message."""


class FeishuWebhookNotConfigured(FeishuWebhookError):
    """The custom bot webhook has not been configured."""


class FeishuWebhookClient:
    def __init__(
        self,
        webhook_url: str | None = None,
        secret: str | None = None,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.webhook_url = (webhook_url or os.getenv("FEISHU_WEBHOOK_URL", "")).strip()
        self.secret = (secret or os.getenv("FEISHU_WEBHOOK_SECRET", "")).strip()
        self.opener = opener

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send_report(self, report: dict[str, Any]) -> None:
        if not self.configured:
            raise FeishuWebhookNotConfigured("未配置 FEISHU_WEBHOOK_URL")
        payload = {"msg_type": "interactive", "card": build_report_card(report)}
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = build_signature(timestamp, self.secret)
        request = Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=15) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise FeishuWebhookError(f"飞书 Webhook 请求失败（HTTP {exc.code}）：{detail}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise FeishuWebhookError(f"飞书 Webhook 请求失败：{exc}") from exc
        if response_data.get("code", 0) != 0:
            message = response_data.get("msg") or response_data.get("message") or "未知错误"
            raise FeishuWebhookError(f"飞书 Webhook 拒绝消息：{message}")


def build_signature(timestamp: str, secret: str) -> str:
    digest = hmac.new(
        f"{timestamp}\n{secret}".encode("utf-8"), b"", hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_report_card(report: dict[str, Any]) -> dict[str, Any]:
    risk_level = str(report.get("risk_level") or "low")
    risk_labels = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
    templates = {"low": "green", "medium": "orange", "high": "red", "critical": "red"}
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**日期**\n{report.get('report_date', '-')}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**风险等级**\n{risk_labels.get(risk_level, risk_level)}（{report.get('risk_score', 0)}）"}},
            ],
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": f"**日报摘要**\n{_text(report.get('executive_summary'))}"},
        {"tag": "markdown", "content": f"**风险依据**\n{_text(report.get('risk_basis'))}"},
    ]
    for heading, field, formatter in (
        ("关键进展", "key_developments", _development_text),
        ("关键风险", "key_risks", _item_text),
        ("建议行动", "recommended_actions", _item_text),
    ):
        entries = report.get(field) or []
        if entries:
            lines = [formatter(item) for item in entries[:5] if isinstance(item, dict)]
            if lines:
                elements.append({"tag": "hr"})
                elements.append({"tag": "markdown", "content": f"**{heading}**\n" + "\n".join(lines)})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": templates.get(risk_level, "grey"),
            "title": {"tag": "plain_text", "content": _text(report.get("title") or "情报日报", 100)},
        },
        "elements": elements,
    }


def _development_text(item: dict[str, Any]) -> str:
    title = _text(item.get("title"), 100)
    finding = _text(item.get("finding"), 350)
    return f"- **{title}**：{finding}{_source_links(item)}"


def _item_text(item: dict[str, Any]) -> str:
    return f"- {_text(item.get('content'), 400)}{_source_links(item)}"


def _source_links(item: dict[str, Any]) -> str:
    sources = item.get("sources") or []
    links = []
    for source in sources[:3]:
        if not isinstance(source, dict):
            continue
        url = str(source.get("source_url") or "").strip()
        if url.startswith(("https://", "http://")):
            links.append(f"[{_text(source.get('title') or source.get('publisher') or '原文', 80)}]({url})")
    return f"\n  原文：{' · '.join(links)}" if links else ""


def _text(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "-").split())[:limit]
