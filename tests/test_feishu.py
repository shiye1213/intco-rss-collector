from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any
from urllib.request import Request

import pytest

from app.feishu import (
    MAX_WEBHOOK_BODY_BYTES,
    FeishuConfigurationError,
    FeishuReportPublisher,
    FeishuWebhookClient,
    FeishuWebhookError,
    build_daily_report_card,
    build_feishu_signature,
)


class RecordingOpener:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float) -> bytes:
        self.calls.append((request, timeout))
        return json.dumps(self.response).encode("utf-8")


def sample_report() -> dict[str, Any]:
    source = {
        "article_id": 17,
        "title": "美国调整医用手套进口政策",
        "publisher": "Example News",
        "source_url": "https://example.com/policy-update",
    }
    return {
        "id": 9,
        "status": "success",
        "feishu_status": "not_pushed",
        "report_date": "2026-07-28",
        "keyword_category_name": "贸易政策",
        "title": "医疗耗材贸易政策日报",
        "risk_level": "high",
        "risk_score": 78,
        "article_count": 1,
        "executive_summary": "医疗耗材进口准入要求发生变化。",
        "risk_basis": "新规存在明确生效日期。",
        "key_developments": [
            {
                "title": "进口政策调整",
                "finding": "监管机构更新了进口要求。",
                "business_impact": "可能影响市场准入节奏。",
                "sources": [source],
            }
        ],
        "key_risks": [
            {
                "content": "现有材料可能需要重新核验。",
                "sources": [source],
            }
        ],
        "opportunities": [],
        "recommended_actions": [],
        "watchlist": [],
        "sources": [source],
    }


def test_build_feishu_signature_matches_official_algorithm() -> None:
    timestamp = 1_469_727_884
    secret = "test-secret"
    key = f"{timestamp}\n{secret}".encode()
    expected = base64.b64encode(
        hmac.new(key, b"", hashlib.sha256).digest()
    ).decode()

    assert build_feishu_signature(timestamp, secret) == expected


def test_webhook_sends_signed_interactive_card() -> None:
    opener = RecordingOpener({"code": 0, "msg": "success", "data": {}})
    client = FeishuWebhookClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        "test-secret",
        timeout=8,
        opener=opener,
        clock=lambda: 1_469_727_884,
    )

    response = client.send_card(build_daily_report_card(sample_report()))

    assert response["code"] == 0
    request, timeout = opener.calls[0]
    body = json.loads(request.data.decode("utf-8"))
    assert timeout == 8
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json; charset=utf-8"
    assert body["msg_type"] == "interactive"
    assert body["timestamp"] == "1469727884"
    assert body["sign"] == build_feishu_signature(
        1_469_727_884, "test-secret"
    )
    assert body["card"]["schema"] == "2.0"


def test_daily_report_card_keeps_category_and_source_links() -> None:
    card = build_daily_report_card(sample_report())
    content = card["body"]["elements"][0]["content"]
    payload = json.dumps(
        {"msg_type": "interactive", "card": card},
        ensure_ascii=False,
    ).encode()

    assert "贸易政策" in card["header"]["title"]["content"]
    assert "出处" in content
    assert "https://example.com/policy-update" in content
    assert len(payload) < MAX_WEBHOOK_BODY_BYTES


def test_webhook_rejects_invalid_host_and_oversized_body() -> None:
    invalid_client = FeishuWebhookClient("https://example.com/hook/token")
    with pytest.raises(FeishuConfigurationError):
        invalid_client.send_card({"schema": "2.0"})

    valid_client = FeishuWebhookClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        opener=RecordingOpener({"code": 0}),
    )
    with pytest.raises(FeishuWebhookError, match="20 KB"):
        valid_client.send_card(
            {
                "schema": "2.0",
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "中" * MAX_WEBHOOK_BODY_BYTES}
                    ]
                },
            }
        )


def test_webhook_treats_nonzero_code_as_failure() -> None:
    client = FeishuWebhookClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        opener=RecordingOpener({"code": 19021, "msg": "sign match fail"}),
    )

    with pytest.raises(FeishuWebhookError, match="19021"):
        client.send_card({"schema": "2.0"})


class FakeRepository:
    def __init__(self) -> None:
        self.report = sample_report()
        self.status_changes: list[tuple[str, str]] = []

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        return self.report if report_id == self.report["id"] else None

    def update_report_feishu_status(
        self, report_id: int, status: str, error_message: str = ""
    ) -> None:
        assert report_id == self.report["id"]
        self.report["feishu_status"] = status
        self.status_changes.append((status, error_message))


def test_report_publisher_records_success_and_failure_separately() -> None:
    repository = FakeRepository()
    success_client = FeishuWebhookClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        opener=RecordingOpener({"code": 0, "msg": "success"}),
    )
    FeishuReportPublisher(repository, success_client).push(9)
    assert repository.status_changes == [("sending", ""), ("success", "")]

    repository.report["feishu_status"] = "not_pushed"
    repository.status_changes.clear()
    failed_client = FeishuWebhookClient(
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        opener=RecordingOpener({"code": 9499, "msg": "Bad Request"}),
    )
    with pytest.raises(FeishuWebhookError):
        FeishuReportPublisher(repository, failed_client).push(9)
    assert repository.status_changes[0] == ("sending", "")
    assert repository.status_changes[1][0] == "failed"
    assert "9499" in repository.status_changes[1][1]
