from __future__ import annotations

import io
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.content import (
    ArticleReference,
    ContentFetchError,
    http_fetch_error,
    validate_public_http_url,
)
from app.llm import OpenAIWebContentReader


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self.response = response
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float):
        self.requests.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def web_response(result: dict, *, include_evidence: bool = True) -> dict:
    output: list[dict] = []
    if include_evidence:
        output.append(
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {
                    "type": "open_page",
                    "url": "https://publisher.example/article",
                    "sources": [
                        {
                            "type": "url",
                            "url": "https://publisher.example/article",
                        }
                    ],
                },
            }
        )
    output.append(
        {
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps(result, ensure_ascii=False),
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://publisher.example/article",
                            "title": "Article",
                        }
                    ],
                }
            ],
        }
    )
    return {"status": "completed", "output": output}


def article_reference() -> ArticleReference:
    return ArticleReference(
        title="Malaysia expands medical glove production",
        publisher="Industry News",
        urls=("https://news.example/article",),
    )


def test_http_failure_classification_distinguishes_permanent_and_transient() -> None:
    not_found = http_fetch_error("全文页面", 404)
    forbidden = http_fetch_error("全文页面", 403)
    unavailable = http_fetch_error("全文页面", 503)

    assert not_found.failure_kind == "http_404"
    assert not_found.retryable is False
    assert forbidden.failure_kind == "http_403"
    assert forbidden.retryable is True
    assert unavailable.failure_kind == "http_503"
    assert unavailable.retryable is True


def test_url_validation_rejects_private_targets_without_dns_lookup() -> None:
    assert validate_public_http_url("https://publisher.example/article") == (
        "https://publisher.example/article"
    )
    with pytest.raises(ContentFetchError, match="内网"):
        validate_public_http_url("http://127.0.0.1/private-news")


def test_openai_reader_uses_hosted_web_search_and_returns_full_text() -> None:
    full_text = "Malaysia expanded medical glove production capacity. " * 20
    opener = FakeOpener(
        FakeResponse(
            web_response(
                {
                    "success": True,
                    "final_url": "https://publisher.example/article",
                    "full_text": full_text,
                    "failure_reason": "",
                }
            )
        )
    )
    reader = OpenAIWebContentReader(
        api_key="test-key",
        base_url="https://www.cctq.ai/v1",
        model="gpt-5.4-mini",
        min_text_chars=100,
        opener=opener,
    )

    document = reader.read(article_reference())

    assert document.full_text == full_text.strip()
    assert document.final_url == "https://publisher.example/article"
    assert document.extractor == "openai-web-search"
    assert document.content_chars == len(document.full_text)
    assert len(document.content_hash) == 64
    request, timeout = opener.requests[0]
    assert request.full_url == "https://www.cctq.ai/v1/responses"
    assert timeout == 180
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == "gpt-5.4-mini"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["store"] is False
    assert "Malaysia expands medical glove production" in payload["input"]


def test_openai_reader_rejects_text_without_server_web_evidence() -> None:
    opener = FakeOpener(
        FakeResponse(
            web_response(
                {
                    "success": True,
                    "final_url": "https://publisher.example/article",
                    "full_text": "invented text " * 50,
                    "failure_reason": "",
                },
                include_evidence=False,
            )
        )
    )
    reader = OpenAIWebContentReader(api_key="test-key", opener=opener)

    with pytest.raises(ContentFetchError) as exc_info:
        reader.read(article_reference())

    assert exc_info.value.failure_kind == "openai_no_web_evidence"


def test_openai_reader_reports_unavailable_article_without_storing_text() -> None:
    opener = FakeOpener(
        FakeResponse(
            web_response(
                {
                    "success": False,
                    "final_url": None,
                    "full_text": None,
                    "failure_reason": "publisher blocked the search service",
                }
            )
        )
    )
    reader = OpenAIWebContentReader(api_key="test-key", opener=opener)

    with pytest.raises(ContentFetchError, match="publisher blocked") as exc_info:
        reader.read(article_reference())

    assert exc_info.value.failure_kind == "openai_web_unavailable"
    assert exc_info.value.retryable is True


def test_openai_reader_distinguishes_provider_search_outage() -> None:
    opener = FakeOpener(
        FakeResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "status": "failed",
                        "action": {"type": "search", "query": "article"},
                    }
                ],
            }
        )
    )
    reader = OpenAIWebContentReader(api_key="test-key", opener=opener)

    with pytest.raises(ContentFetchError) as exc_info:
        reader.read(article_reference())

    assert exc_info.value.failure_kind == "openai_web_search_unavailable"
    assert exc_info.value.retryable is True


def test_openai_reader_classifies_api_rate_limit() -> None:
    error = HTTPError(
        "https://www.cctq.ai/v1/responses",
        429,
        "Too Many Requests",
        None,
        io.BytesIO(b'{"error":"rate limited"}'),
    )
    reader = OpenAIWebContentReader(
        api_key="test-key",
        opener=FakeOpener(error),
    )

    with pytest.raises(ContentFetchError) as exc_info:
        reader.read(article_reference())

    assert exc_info.value.failure_kind == "openai_http_429"
    assert exc_info.value.retryable is True
