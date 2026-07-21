from __future__ import annotations

import json
from email.message import Message
from urllib.request import Request

from app.content import GoogleNewsURLResolver, WebContentFetcher


class FakeResponse:
    def __init__(
        self,
        body: str,
        *,
        url: str = "https://publisher.example/article",
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self._body = body.encode("utf-8")
        self._url = url
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def geturl(self) -> str:
        return self._url


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float):
        self.requests.append(request)
        return self.responses.pop(0)


class StaticResolver:
    def resolve(self, url: str) -> str:
        return url


def test_google_news_resolver_returns_publisher_url(monkeypatch) -> None:
    html = (
        '<html><body><c-wiz><div jscontroller="abc" '
        'data-n-a-sg="signature-123" data-n-a-ts="1784617053"></div>'
        "</c-wiz></body></html>"
    )
    rpc_result = json.dumps(
        [
            [
                "wrb.fr",
                "Fbv4je",
                json.dumps(
                    ["garturlres", "https://publisher.example/article", 1]
                ),
            ]
        ]
    )
    opener = FakeOpener([FakeResponse(html), FakeResponse(")]}'\n\n" + rpc_result)])
    monkeypatch.setattr("app.content.validate_public_http_url", lambda url: url)
    resolver = GoogleNewsURLResolver(opener=opener)

    result = resolver.resolve(
        "https://news.google.com/rss/articles/opaque-article-id?oc=5"
    )

    assert result == "https://publisher.example/article"
    assert len(opener.requests) == 2
    assert opener.requests[0].full_url.endswith("/articles/opaque-article-id")
    assert opener.requests[1].data is not None
    assert b"Fbv4je" in opener.requests[1].data


def test_web_content_fetcher_extracts_article_text_without_navigation(
    monkeypatch,
) -> None:
    article_text = "医院计划增加一次性丁腈手套采购，以保障临床防护物资供应。" * 20
    html = f"""
    <html lang="zh-CN">
      <head><title>医院手套采购计划</title></head>
      <body>
        <nav>系统登录 首页 产品中心 联系我们</nav>
        <article><h1>医院手套采购计划</h1><p>{article_text}</p></article>
        <footer>版权信息 隐私条款</footer>
      </body>
    </html>
    """
    opener = FakeOpener([FakeResponse(html)])
    monkeypatch.setattr("app.content.validate_public_http_url", lambda url: url)
    fetcher = WebContentFetcher(
        opener=opener,
        url_resolver=StaticResolver(),
        min_text_chars=100,
    )

    document = fetcher.fetch("https://publisher.example/article")

    assert "增加一次性丁腈手套采购" in document.full_text
    assert "系统登录" not in document.full_text
    assert document.content_chars == len(document.full_text)
    assert len(document.content_hash) == 64
