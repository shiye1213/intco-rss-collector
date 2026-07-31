from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable

from .prompts import CATEGORY_LABELS
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_CARD_SIZE = 28_000
MAX_MARKDOWN_BLOCK_SIZE = 6_000


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
        self.webhook_url = (
            os.getenv("FEISHU_WEBHOOK_URL", "")
            if webhook_url is None
            else webhook_url
        ).strip()
        self.secret = (
            os.getenv("FEISHU_WEBHOOK_SECRET", "")
            if secret is None
            else secret
        ).strip()
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
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**相关性**\n{risk_labels.get(risk_level, risk_level)}（{report.get('risk_score', 0)}）"}},
            ],
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": f"**今日总体总结**\n{_text(report.get('executive_summary'))}"},
        {"tag": "markdown", "content": f"**整体风险依据**\n{_text(report.get('risk_basis'))}"},
    ]
    developments = [
        item
        for item in (report.get("key_developments") or [])
        if isinstance(item, dict)
    ]
    if developments:
        elements.append({"tag": "hr"})
        _append_markdown_chunks(
            elements,
            "逐条新闻分析",
            [_development_text(item) for item in developments],
        )
    for heading, field in (
        ("关键风险", "key_risks"),
        ("业务机会", "opportunities"),
        ("建议行动", "recommended_actions"),
        ("后续监控", "watchlist"),
    ):
        entries = [
            item for item in (report.get(field) or []) if isinstance(item, dict)
        ]
        if entries:
            elements.append({"tag": "hr"})
            _append_markdown_chunks(
                elements,
                heading,
                [_item_text(item) for item in entries],
            )
    _limit_card_size(elements)
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
    finding = _text(item.get("finding") or "暂未明确", 600)
    region = _text(item.get("affected_region") or "暂未明确", 200)
    products = _text(item.get("products") or "暂未明确", 200)
    reason = _text(item.get("impact_reason") or "暂未明确", 600)
    impact = _text(item.get("business_impact") or "暂未明确", 1_000)
    action = _text(item.get("recommended_action") or "暂未明确", 600)
    risk_labels = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
    risk = risk_labels.get(str(item.get("risk_level") or "low"), "低")
    category_code = str(item.get("category") or "other")
    category = _text(CATEGORY_LABELS.get(category_code, category_code), 100)
    return (
        f"**{title}**\n"
        f"新闻类型：{category}　影响地区：{region}\n"
        f"涉及产品：{products}　影响等级：{risk}（{item.get('risk_score', 0)}）\n"
        f"核心事实：{finding}\n"
        f"影响原因：{reason}\n"
        f"业务影响：{impact}\n"
        f"建议措施：{action}{_source_links(item)}"
    )


def _item_text(item: dict[str, Any]) -> str:
    return f"- {_text(item.get('content'), 1_000)}{_source_links(item)}"


def _append_markdown_chunks(
    elements: list[dict[str, Any]], heading: str, lines: list[str]
) -> None:
    chunk = f"**{heading}**\n"
    for line in lines:
        candidate = f"{chunk}{line}" if chunk.endswith("\n") else f"{chunk}\n{line}"
        if len(candidate) > MAX_MARKDOWN_BLOCK_SIZE and chunk != f"**{heading}**\n":
            elements.append({"tag": "markdown", "content": chunk})
            chunk = f"**{heading}（续）**\n{line}"
        else:
            chunk = candidate
    if chunk.strip():
        elements.append({"tag": "markdown", "content": chunk})


def _limit_card_size(elements: list[dict[str, Any]]) -> None:
    """Keep custom-bot payloads under Feishu's practical 30 KB card limit."""
    size = len(json.dumps(elements, ensure_ascii=False).encode("utf-8"))
    truncated = False
    while size > MAX_CARD_SIZE and len(elements) > 5:
        elements.pop()
        truncated = True
        if elements and elements[-1].get("tag") == "hr":
            elements.pop()
        size = len(json.dumps(elements, ensure_ascii=False).encode("utf-8"))
    if size > MAX_CARD_SIZE:
        raise FeishuWebhookError("飞书卡片内容超过大小限制，无法推送")
    if truncated:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "日报内容过长，部分末尾区块未能发送。",
                    }
                ],
            }
        )


def _source_links(item: dict[str, Any]) -> str:
    sources = item.get("sources") or []
    valid_sources = []
    for source in sources[:3]:
        if not isinstance(source, dict):
            continue
        url = str(source.get("source_url") or "").strip()
        if url.startswith(("https://", "http://")):
            valid_sources.append((source, url))
    links = [
        f"[{_short_source_title(source)}]({url})"
        for source, url in valid_sources
    ]
    return f"\n  原文：{' · '.join(links)}" if links else ""


def _short_source_title(source: dict[str, Any], limit: int = 24) -> str:
    value = source.get("title") or source.get("publisher") or "原文"
    title = " ".join(str(value).split()).replace("[", "【").replace("]", "】")
    return f"{title[:limit]}…" if len(title) > limit else title


def _text(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "-").split())[:limit]
