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
        "executive_summary": "采购规则变化可能提高准入成本。",
        "risk_basis": "政策尚处于征求意见阶段。",
        "key_developments": [
            {
                "title": "准入规则更新",
                "category": "政策法规",
                "affected_region": "美国",
                "products": "一次性手套",
                "risk_level": "high",
                "risk_score": 72,
                "finding": "规则扩大本地化要求",
                "impact_reason": "本地化门槛提高",
                "business_impact": "准入成本可能上升",
                "recommended_action": "由法规部门核对适用范围",
                "sources": [{"title": "监管原文", "source_url": "https://example.com/source"}],
            }
        ],
        "key_risks": [{"content": "合规成本可能上升", "sources": [{"publisher": "监管机构", "source_url": "https://example.com/risk"}]}],
        "recommended_actions": [{"content": "评估受影响产品", "sources": [{"title": "行动依据", "source_url": "https://example.com/action"}]}],
    }


def test_signature_uses_feishu_hmac_format() -> None:
    expected = base64.b64encode(hmac.new(b"123\nsecret", b"", hashlib.sha256).digest()).decode("ascii")
    assert build_signature("123", "secret") == expected


def test_report_card_contains_required_sections_and_source_links() -> None:
    card = build_report_card(sample_report())
    content = "\n".join(element.get("content", "") for element in card["elements"])
    assert card["header"]["template"] == "red"
    assert "今日总体总结" in content
    assert "整体风险依据" in content
    assert "逐条新闻分析" in content
    assert "**准入规则更新**" in content
    assert "新闻类型：政策法规" in content
    assert "影响地区：美国" in content
    assert "涉及产品：一次性手套" in content
    assert "核心事实：规则扩大本地化要求" in content
    assert "影响原因：本地化门槛提高" in content
    assert "业务影响：准入成本可能上升" in content
    assert "建议措施：由法规部门核对适用范围" in content
    assert "关键风险" in content
    assert "业务机会" not in content
    assert "建议行动" in content
    assert "[监管原文](https://example.com/source)" in content
    assert "[查看原文](https://example.com/source)" not in content


def test_report_card_uses_shortened_news_titles_for_source_links() -> None:
    report = sample_report()
    long_title = "这是一个用于验证日报出处链接自动截断效果的较长新闻标题"
    report["key_developments"][0]["sources"].append(
        {"title": long_title, "source_url": "https://example.com/source-2"}
    )

    card = build_report_card(report)
    content = "\n".join(
        element.get("content", "") for element in card["elements"]
    )

    assert "[监管原文](https://example.com/source)" in content
    assert f"[{long_title[:24]}…](https://example.com/source-2)" in content
    assert long_title not in content


def test_report_card_keeps_all_developments_until_the_card_size_limit() -> None:
    report = sample_report()
    report["key_developments"] = [
        {
            "title": f"风险事项 {index}",
            "finding": "完整事实",
            "business_impact": "完整解读",
        }
        for index in range(9)
    ]

    card = build_report_card(report)
    content = "\n".join(element.get("content", "") for element in card["elements"])

    assert "风险事项 8" in content


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


def test_webhook_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
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
