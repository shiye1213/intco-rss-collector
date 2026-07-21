from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from app.collector import (
    Collector,
    build_feed_url,
    canonicalize_url,
    parse_feed,
)
from app.database import Database
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


def test_keyword_query_is_generated_only_from_match_terms() -> None:
    query = build_keyword_query(
        ["PE手套", "聚乙烯手套", "polyethylene gloves", "PE手套"]
    )

    assert query == (
        '("PE手套" OR "聚乙烯手套" OR "polyethylene gloves")'
    )
    assert "medical" not in query
    assert "-football" not in query


def test_first_run_uses_today_and_second_run_uses_cursor(tmp_path) -> None:
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
