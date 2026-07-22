from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.collector import (
    Collector,
    build_feed_url,
    canonicalize_url,
    parse_feed,
)
from app.database import DEFAULT_SOURCES, Database
from app.query_builder import build_keyword_query


RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test</title>
    <item>
      <title>Polyethylene gloves market update</title>
      <link>https://example.com/news/1?utm_source=rss</link>
      <source>Example News</source>
      <guid>example-news-1</guid>
      <category>Medical Devices</category>
      <category>Gloves</category>
      <pubDate>Mon, 20 Jul 2026 02:00:00 GMT</pubDate>
      <description><![CDATA[Medical polyethylene gloves demand increased.]]></description>
    </item>
  </channel>
</rss>
"""


ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom</title>
  <entry>
    <id>atom-item-1</id>
    <title>PE glove regulation</title>
    <link href="https://example.org/item" rel="alternate" />
    <updated>2026-07-20T03:00:00Z</updated>
    <author><name>Example Authority</name></author>
    <category term="Regulation" />
    <summary>New polyethylene gloves guidance.</summary>
  </entry>
</feed>
"""


EMPTY_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>
"""


def configured_database(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": "测试源",
            "url_template": "https://example.com/feed.xml",
            "mode": "direct",
            "language": "en-US",
            "country": "US",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "测试 PE 手套",
            "query": '"polyethylene gloves"',
            "match_terms": ["polyethylene gloves", "PE gloves"],
            "active": True,
        }
    )
    return database


def test_parse_rss_and_atom() -> None:
    rss_items = parse_feed(RSS_XML)
    atom_items = parse_feed(ATOM_XML)

    assert len(rss_items) == 1
    assert rss_items[0].publisher == "Example News"
    assert rss_items[0].guid == "example-news-1"
    assert rss_items[0].categories == ("Medical Devices", "Gloves")
    assert rss_items[0].published_at == datetime(2026, 7, 20, 2, tzinfo=UTC)
    assert len(atom_items) == 1
    assert atom_items[0].title == "PE glove regulation"
    assert atom_items[0].publisher == "Example Authority"
    assert atom_items[0].guid == "atom-item-1"
    assert atom_items[0].categories == ("Regulation",)
    assert atom_items[0].published_at == datetime(2026, 7, 20, 3, tzinfo=UTC)


def test_build_search_url_and_canonicalize_tracking_parameters() -> None:
    source = {
        "mode": "search",
        "url_template": "https://example.com/rss?q={query}",
    }
    keyword = {"query": '"PE gloves" OR "polyethylene gloves"'}
    url = build_feed_url(source, keyword)

    assert "%22PE+gloves%22" in url
    assert canonicalize_url("https://EXAMPLE.com/news/1/?utm_source=rss&x=1#top") == (
        "https://example.com/news/1?x=1"
    )


def test_default_sources_include_site_limited_malaysia_publishers() -> None:
    sources = {item["name"]: item for item in DEFAULT_SOURCES}

    the_star = sources["Google News 马来西亚 The Star"]
    the_edge = sources["Google News 马来西亚 The Edge"]
    assert "site%3Athestar.com.my+{query}" in the_star["url_template"]
    assert "site%3Atheedgemalaysia.com+{query}" in the_edge["url_template"]
    assert the_star["country"] == the_edge["country"] == "MY"


def test_keyword_query_is_generated_only_from_match_terms() -> None:
    query = build_keyword_query(
        ["PE手套", "聚乙烯手套", "polyethylene gloves", "PE手套"]
    )

    assert query == (
        '("PE手套" OR "聚乙烯手套" OR "polyethylene gloves")'
    )
    assert "medical" not in query
    assert "-football" not in query


def test_keyword_query_combines_subjects_signals_exclusions_and_recency() -> None:
    query = build_keyword_query(
        ["nitrile gloves", "medical gloves", "NITRILE GLOVES"],
        context_terms=["tariff", "regulation", "Tariff"],
        exclude_terms=["boxing gloves", "football", "FOOTBALL"],
        lookback_days=30,
    )

    assert query == (
        '("nitrile gloves" OR "medical gloves") '
        'AND ("tariff" OR "regulation") '
        '-"boxing gloves" -"football" when:30d'
    )


def test_search_sources_receive_only_matching_language_terms(tmp_path) -> None:
    database = Database(tmp_path / "language-routing.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": "Chinese search",
            "url_template": "https://zh.example/rss?q={query}",
            "mode": "search",
            "language": "zh-CN",
            "country": "CN",
            "active": True,
        }
    )
    database.create_source(
        {
            "name": "English search",
            "url_template": "https://en.example/rss?q={query}",
            "mode": "search",
            "language": "en-US",
            "country": "US",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Mixed gloves",
            "match_terms": ["医用手套", "medical gloves"],
            "context_terms": ["采购", "procurement"],
            "exclude_terms": ["拳击", "boxing"],
            "lookback_days": 14,
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Chinese only",
            "match_terms": ["丁腈手套"],
            "context_terms": ["价格"],
            "exclude_terms": [],
            "lookback_days": 14,
            "active": True,
        }
    )
    requested_urls: list[str] = []

    def fake_fetch(url: str, _timeout: float) -> bytes:
        requested_urls.append(url)
        return EMPTY_RSS_XML

    collector = Collector(database, feed_fetcher=fake_fetch)
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-07-21T16:00:00Z"
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["tasks_total"] == 3
    assert len(requested_urls) == 3
    queries = [
        ((urlsplit(url).hostname or ""), parse_qs(urlsplit(url).query)["q"][0])
        for url in requested_urls
    ]
    chinese_queries = [query for host, query in queries if host == "zh.example"]
    english_queries = [query for host, query in queries if host == "en.example"]
    assert len(chinese_queries) == 2
    assert len(english_queries) == 1
    assert all("medical gloves" not in query for query in chinese_queries)
    assert all("procurement" not in query for query in chinese_queries)
    assert "医用手套" not in english_queries[0]
    assert "采购" not in english_queries[0]


def test_direct_source_matches_only_keywords_for_its_language(tmp_path) -> None:
    database = Database(tmp_path / "direct-language-routing.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": "English direct",
            "url_template": "https://en.example/feed.xml",
            "mode": "direct",
            "language": "en-US",
            "country": "US",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Mixed gloves",
            "match_terms": ["医用手套", "polyethylene gloves"],
            "lookback_days": 14,
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Chinese only",
            "match_terms": ["丁腈手套"],
            "lookback_days": 14,
            "active": True,
        }
    )
    collector = Collector(database, feed_fetcher=lambda _url, _timeout: RSS_XML)
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-07-21T16:00:00Z"
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["tasks_total"] == 1
    assert result["items_inserted"] == 1
    assert len(result["details"]) == 1
    assert result["details"][0]["keyword_name"] == "Mixed gloves"


def test_keyword_search_strategy_is_persisted_and_regenerates_query(tmp_path) -> None:
    database = Database(tmp_path / "keyword-strategy.db")
    database.initialize()
    keyword_id = database.create_keyword(
        {
            "name": "测试医疗手套情报",
            "match_terms": ["nitrile gloves", "medical gloves"],
            "context_terms": ["tariff", "demand"],
            "exclude_terms": ["boxing", "football"],
            "lookback_days": 14,
            "active": True,
        }
    )

    keyword = next(item for item in database.get_keywords() if item["id"] == keyword_id)

    assert keyword["context_terms"] == ["tariff", "demand"]
    assert keyword["exclude_terms"] == ["boxing", "football"]
    assert keyword["lookback_days"] == 14
    assert keyword["query"] == (
        '("nitrile gloves" OR "medical gloves") '
        'AND ("tariff" OR "demand") -"boxing" -"football" when:14d'
    )
    feed_url = build_feed_url(
        {"mode": "search", "url_template": "https://example.com/rss?q={query}"},
        keyword,
    )
    assert "{query}" not in feed_url
    assert "when%3A14d" in feed_url
    assert "-%22boxing%22" in feed_url


def test_keyword_query_rejects_oversized_google_expression() -> None:
    with pytest.raises(ValueError, match="查询过长"):
        build_keyword_query(
            [f"very specific medical product phrase {index}" for index in range(20)],
            context_terms=[f"business signal phrase {index}" for index in range(20)],
            lookback_days=30,
        )


def test_second_run_uses_cursor_after_initial_collection(tmp_path) -> None:
    database = configured_database(tmp_path)

    def fake_fetch(_: str, __: float) -> bytes:
        return RSS_XML

    collector = Collector(database, feed_fetcher=fake_fetch)
    first_started = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
    first_run = database.create_run(
        "manual", first_started.isoformat(), "2026-07-19T16:00:00Z"
    )
    collector.collect(first_run, first_started)

    first_result = database.get_run(first_run)
    assert first_result is not None
    assert first_result["status"] == "success"
    assert first_result["items_inserted"] == 1
    assert database.article_count() == 1

    second_started = first_started + timedelta(hours=1)
    second_run = database.create_run(
        "manual", second_started.isoformat(), first_started.isoformat()
    )
    collector.collect(second_run, second_started)

    second_result = database.get_run(second_run)
    assert second_result is not None
    assert second_result["status"] == "success"
    assert second_result["items_inserted"] == 0
    assert second_result["items_matched"] == 0
    assert second_result["details"][0]["skipped_outside_window"] == 1
    assert database.article_count() == 1


def test_new_keyword_uses_its_google_news_lookback_window(tmp_path) -> None:
    database = configured_database(tmp_path)
    keyword = database.get_keywords(active_only=True)[0]
    database.update_keyword(
        keyword["id"],
        {
            **keyword,
            "lookback_days": 14,
        },
    )
    collector = Collector(database, feed_fetcher=lambda _url, _timeout: RSS_XML)
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-07-21T16:00:00Z"
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["items_inserted"] == 1
    assert result["details"][0]["window_start"] == "2026-07-08T16:00:00Z"


def test_failed_source_does_not_advance_cursor(tmp_path) -> None:
    database = configured_database(tmp_path)

    def broken_fetch(_: str, __: float) -> bytes:
        raise TimeoutError("network timeout")

    collector = Collector(database, feed_fetcher=broken_fetch)
    started = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
    run_id = database.create_run("manual", started.isoformat(), "2026-07-19T16:00:00Z")
    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["status"] == "failed"
    assert result["tasks_failed"] == 1
    with database.connect() as connection:
        cursor_count = connection.execute(
            "SELECT COUNT(*) FROM collection_cursors"
        ).fetchone()[0]
    assert cursor_count == 0


def test_duplicate_article_keeps_all_source_metadata(tmp_path) -> None:
    database = configured_database(tmp_path)
    second_source_id = database.create_source(
        {
            "name": "测试源二",
            "url_template": "https://example.org/feed.xml",
            "mode": "direct",
            "language": "en-CA",
            "country": "CA",
            "active": True,
        }
    )

    collector = Collector(database, feed_fetcher=lambda _url, _timeout: RSS_XML)
    started = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-07-19T16:00:00Z"
    )
    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["items_inserted"] == 1
    assert result["duplicates"] == 1
    articles = database.list_articles(source_id=second_source_id)
    assert articles["total"] == 1
    article = articles["items"][0]
    assert article["publisher_normalized"] == "Example News"
    assert len(article["sources"]) == 2
    assert article["source_names"] == ["测试源", "测试源二"]
    assert article["languages"] == ["en-US", "en-CA"]
    assert article["countries"] == ["US", "CA"]
    assert article["categories"] == ["Medical Devices", "Gloves"]
    assert {source["guid"] for source in article["sources"]} == {"example-news-1"}


def test_initialize_migrates_existing_article_metadata(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE rss_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                url_template TEXT NOT NULL,
                mode TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                publisher TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                rss_source_id INTEGER REFERENCES rss_sources(id)
            );
            INSERT INTO rss_sources
                (name, url_template, mode, language, active, archived,
                 created_at, updated_at)
            VALUES
                ('Legacy Google',
                 'https://news.google.com/rss/search?q={query}&gl=US',
                 'search', 'en-US', 1, 0,
                 '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z');
            INSERT INTO articles
                (title, url, canonical_url, fingerprint, publisher, summary,
                 published_at, collected_at, rss_source_id)
            VALUES
                ('Legacy article', 'https://example.com/legacy',
                 'https://example.com/legacy', 'legacy-fingerprint',
                 '  Example   News  ', '', '2026-07-20T01:00:00Z',
                 '2026-07-20T02:00:00Z', 1);
            """
        )

    database = Database(database_path)
    database.initialize()

    with database.connect() as connection:
        source = connection.execute(
            "SELECT country FROM rss_sources WHERE id = 1"
        ).fetchone()
        article = connection.execute(
            "SELECT publisher_normalized FROM articles WHERE id = 1"
        ).fetchone()
        provenance = connection.execute(
            "SELECT language, country FROM article_sources WHERE article_id = 1"
        ).fetchone()
    assert source["country"] == "US"
    assert article["publisher_normalized"] == "Example News"
    assert dict(provenance) == {"language": "en-US", "country": "US"}
