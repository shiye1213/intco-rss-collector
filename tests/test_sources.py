from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.main import SourcePayload


def test_search_source_normalizes_site_domain() -> None:
    source = SourcePayload(
        name="Reuters",
        url_template="https://news.google.com/rss/search?q={query}",
        mode="search",
        site_domain="https://Reuters.COM/",
    )

    source.validate_mode_template()
    assert source.site_domain == "reuters.com"


def test_search_source_normalizes_multiple_site_domains() -> None:
    source = SourcePayload(
        name="Multi-site news",
        url_template="https://news.google.com/rss/search?q={query}",
        mode="search",
        site_domain=(
            "https://Reuters.COM/\nsite:CLS.cn，wallstreetcn.com OR reuters.com"
        ),
    )

    source.validate_mode_template()
    assert source.site_domain == "reuters.com OR cls.cn OR wallstreetcn.com"


def test_site_domain_rejects_paths() -> None:
    with pytest.raises(ValidationError, match="reuters.com"):
        SourcePayload(
            name="Reuters",
            url_template="https://news.google.com/rss/search?q={query}",
            mode="search",
            site_domain="reuters.com/world",
        )


def test_direct_source_rejects_site_domain() -> None:
    source = SourcePayload(
        name="Direct feed",
        url_template="https://example.com/feed.xml",
        mode="direct",
        site_domain="example.com",
    )

    with pytest.raises(ValueError, match="只适用于搜索型"):
        source.validate_mode_template()


def test_crawler_source_accepts_fixed_news_page() -> None:
    source = SourcePayload(
        name="Example news crawler",
        url_template="https://example.com/news/",
        mode="crawler",
        language="en-US",
    )

    source.validate_mode_template()
    assert source.mode == "crawler"


def test_crawler_source_rejects_query_placeholder() -> None:
    source = SourcePayload(
        name="Invalid crawler",
        url_template="https://example.com/news/?q={query}",
        mode="crawler",
    )

    with pytest.raises(ValueError, match="固定的新闻列表页"):
        source.validate_mode_template()
