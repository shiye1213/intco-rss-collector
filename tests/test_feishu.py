import base64
import hashlib
import hmac
import json

import pytest

from app.feishu import (
    FeishuWebhookClient,
    FeishuWebhookError,
    FeishuWebhookNotConfigured,
    build_report_card,
    build_signature,
)


def sample_report() -> dict:
    return {
        "id": 3,
        "title": "2026-07-28 贸易政策日报",
        "report_date": "2026-07-28",
        "risk_level": "high",
        "risk_score": 72,
        "overview": "采购规则变化可能提高准入成本，政策尚处于征求意见阶段。",
        "details": [
            {
                "title": "准入规则变化",
                "content": "规则扩大本地化要求，合规成本可能上升。",
                "sources": [
                    {
                        "title": "监管原文",
                        "source_url": "https://example.com/source",
                    }
                ],
            }
        ],
    }


def test_signature_uses_feishu_hmac_format() -> None:
    expected = base64.b64encode(hmac.new(b"123\nsecret", b"", hashlib.sha256).digest()).decode("ascii")
    assert build_signature("123", "secret") == expected


def test_report_card_contains_required_sections_and_source_links() -> None:
    card = build_report_card(sample_report())
    content = "\n".join(element.get("content", "") for element in card["elements"])
    assert card["header"]["template"] == "red"
    assert "总体概括" in content
    assert "详细解读" in content
    assert "管理层摘要" not in content
    assert "关键进展" not in content
    assert "建议行动" not in content
    assert "[来源 1](https://example.com/source)" in content
    assert "监管原文" not in content


def test_webhook_sends_interactive_card_and_signature() -> None:
    captured = {}

    class Response:
        def read(self): return b'{"code": 0}'
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    client = FeishuWebhookClient("https://example.com/hook", "secret", opener)
    client.send_report(sample_report())
    payload = json.loads(captured["request"].data.decode("utf-8"))
    assert captured["request"].get_header("Content-type") == "application/json; charset=utf-8"
    assert payload["msg_type"] == "interactive"
    assert payload["sign"] == build_signature(payload["timestamp"], "secret")


def test_webhook_requires_url() -> None:
    with pytest.raises(FeishuWebhookNotConfigured):
        FeishuWebhookClient("", "").send_report(sample_report())


def test_feishu_error_is_reported_when_response_has_error_code() -> None:
    class Response:
        def read(self): return b'{"code": 19021, "msg": "signature invalid"}'
        def __enter__(self): return self
        def __exit__(self, *_): return False

    with pytest.raises(FeishuWebhookError, match="signature invalid"):
        FeishuWebhookClient(
            "https://example.com/hook", opener=lambda *_, **__: Response()
        ).send_report(sample_report())
