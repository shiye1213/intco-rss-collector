from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.collector import (
    Collector,
    build_feed_url,
    canonicalize_url,
    extract_crawled_article,
    parse_feed,
)
from app.database import DEFAULT_KEYWORDS, DEFAULT_SOURCES, Database
from app.prompts import (
    DEFAULT_BUSINESS_PROFILE,
    DEFAULT_REPORT_CATEGORY_PROMPTS,
    DEFAULT_RELEVANCE_PROMPT,
    DEFAULT_REPORT_PROMPT,
    LEGACY_DEFAULT_BUSINESS_PROFILE_V4,
    LEGACY_DEFAULT_RELEVANCE_PROMPT_V4,
    LEGACY_DEFAULT_REPORT_PROMPT_V4,
    REPORT_CATEGORY_SETTING_KEYS,
)
from app.query_builder import (
    build_keyword_query,
    localize_keyword_for_source,
)


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

CRAWL_INDEX_HTML = b"""<!doctype html>
<html><head>
  <title>Industry news</title>
  <base href="https://untrusted.example/" />
</head><body>
  <nav><a href="/about">About this website</a></nav>
  <main>
    <a href="/news/2026/07/20/nitrile-gloves-tariff">
      Nitrile gloves tariff update in the United States
    </a>
    <a href="/news/2026/07/19/football-gloves">
      Football gloves return for the new season
    </a>
    <a href="https://external.example/news/2026/07/20/other">
      External article that must not be crawled
    </a>
  </main>
</body></html>
"""

CRAWL_ARTICLE_HTML = b"""<!doctype html>
<html><head>
  <meta property="og:title" content="Nitrile gloves tariff update" />
  <meta property="og:description"
        content="Nitrile gloves imports face a revised tariff." />
  <meta property="og:site_name" content="Example Policy News" />
  <meta property="article:published_time" content="2026-07-20T02:00:00Z" />
</head><body><article><h1>Nitrile gloves tariff update</h1></article></body></html>
"""

CRAWL_IRRELEVANT_HTML = b"""<!doctype html>
<html><head>
  <meta property="og:title" content="Football gloves return" />
  <meta name="description" content="The new sports season begins." />
  <meta property="article:published_time" content="2026-07-19T03:00:00Z" />
</head><body><article><h1>Football gloves return</h1></article></body></html>
"""

CRAWL_JSON_LD_HTML = b"""<!doctype html>
<html><head>
  <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "NewsArticle",
      "headline": "Medical gloves procurement rule",
      "description": "A public procurement rule was updated.",
      "datePublished": "2026-07-21T06:30:00Z",
      "url": "https://example.com/news/procurement-rule",
      "publisher": {"@type": "Organization", "name": "Example Authority"},
      "articleSection": "Procurement"
    }
  </script>
</head><body><h1>Medical gloves procurement rule</h1></body></html>
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


def test_extract_crawled_article_reads_metadata() -> None:
    item = extract_crawled_article(
        CRAWL_ARTICLE_HTML,
        "https://example.com/news/2026/07/20/nitrile-gloves-tariff",
    )

    assert item is not None
    assert item.title == "Nitrile gloves tariff update"
    assert item.publisher == "Example Policy News"
    assert item.summary == "Nitrile gloves imports face a revised tariff."
    assert item.published_at == datetime(2026, 7, 20, 2, tzinfo=UTC)


def test_extract_crawled_article_reads_json_ld() -> None:
    item = extract_crawled_article(
        CRAWL_JSON_LD_HTML,
        "https://example.com/news/procurement-rule",
    )

    assert item is not None
    assert item.title == "Medical gloves procurement rule"
    assert item.publisher == "Example Authority"
    assert item.published_at == datetime(2026, 7, 21, 6, 30, tzinfo=UTC)
    assert item.categories == ("Procurement",)


@pytest.mark.parametrize("mode", ["crawler", "direct"])
def test_web_crawler_mode_and_html_fallback_collect_articles(
    tmp_path,
    mode: str,
) -> None:
    database = Database(tmp_path / f"{mode}-source.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": f"{mode} source",
            "url_template": "https://example.com/news/",
            "mode": mode,
            "language": "en-US",
            "country": "US",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Nitrile gloves",
            "match_terms": ["nitrile gloves"],
            "lookback_days": 30,
            "active": True,
        }
    )
    pages = {
        "https://example.com/news/": CRAWL_INDEX_HTML,
        (
            "https://example.com/news/2026/07/20/nitrile-gloves-tariff"
        ): CRAWL_ARTICLE_HTML,
        "https://example.com/news/2026/07/19/football-gloves": (
            CRAWL_IRRELEVANT_HTML
        ),
    }

    collector = Collector(
        database,
        feed_fetcher=lambda url, _timeout: pages[url],
    )
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual",
        started.isoformat(),
        "2026-06-22T00:00:00Z",
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["status"] == "success"
    assert result["items_seen"] == 2
    assert result["items_matched"] == 1
    assert result["items_inserted"] == 1
    assert database.get_sources(active_only=True)[0]["mode"] == mode
    article = database.list_articles()["items"][0]
    assert article["url"].endswith("/nitrile-gloves-tariff")
    assert article["publisher"] == "Example Policy News"


def test_build_search_url_and_canonicalize_tracking_parameters() -> None:
    source = {
        "mode": "search",
        "url_template": "https://example.com/rss?q={query}",
        "site_domain": "reuters.com",
    }
    keyword = {"query": '("PE gloves"OR"polyethylene gloves")'}
    url = build_feed_url(source, keyword)

    assert parse_qs(urlsplit(url).query)["q"] == [
        'site:reuters.com ("PE gloves"OR"polyethylene gloves")'
    ]
    assert "site%3Areuters.com+" in url
    assert canonicalize_url("https://EXAMPLE.com/news/1/?utm_source=rss&x=1#top") == (
        "https://example.com/news/1?x=1"
    )


def test_build_search_url_combines_multiple_sites_with_or() -> None:
    source = {
        "mode": "search",
        "url_template": "https://example.com/rss?q={query}",
        "site_domain": "reuters.com OR wallstreetcn.com OR cls.cn",
    }
    keyword = {"query": '("tariff"OR"关税")'}

    url = build_feed_url(source, keyword)

    assert parse_qs(urlsplit(url).query)["q"] == [
        '(site:reuters.com OR site:wallstreetcn.com OR site:cls.cn) '
        '("tariff"OR"关税")'
    ]


def test_default_sources_include_site_limited_malaysia_publishers() -> None:
    sources = {item["name"]: item for item in DEFAULT_SOURCES}

    the_star = sources["Google News 马来西亚 The Star"]
    the_edge = sources["Google News 马来西亚 The Edge"]
    assert "site%3Athestar.com.my+{query}" in the_star["url_template"]
    assert "site%3Atheedgemalaysia.com+{query}" in the_edge["url_template"]
    assert the_star["country"] == the_edge["country"] == "MY"


def test_default_tariff_keyword_strategies_are_valid_and_language_specific(
    tmp_path,
) -> None:
    chinese_names = {
        "医疗手套关税（中文）",
        "美国关税法律工具（中文）",
        "贸易救济案件（中文）",
        "手套原材料关税（中文）",
        "通用关税政策（中文）",
    }
    english_names = {
        "医疗手套关税（英文）",
        "美国关税法律工具（英文）",
        "贸易救济案件（英文）",
        "手套原材料关税（英文）",
        "通用关税政策（英文）",
    }
    strategies = {
        item["name"]: item
        for item in DEFAULT_KEYWORDS
        if item["name"] in chinese_names | english_names
    }

    assert set(strategies) == chinese_names | english_names
    assert strategies["通用关税政策（中文）"]["match_terms"] == ["关税"]
    assert strategies["通用关税政策（英文）"]["match_terms"] == ["tariff"]
    assert strategies["医疗手套关税（中文）"]["context_terms"] == ["关税"]
    assert strategies["贸易救济案件（中文）"]["match_terms"] == [
        "反倾销",
        "反补贴",
        "保障措施",
        "贸易救济",
    ]
    assert strategies["贸易救济案件（英文）"]["match_terms"] == [
        "anti-dumping",
        "countervailing",
        "safeguard",
        "trade remedy",
    ]
    for name, strategy in strategies.items():
        query = build_keyword_query(
            strategy["match_terms"],
            context_terms=strategy["context_terms"],
            exclude_terms=strategy["exclude_terms"],
            lookback_days=strategy["lookback_days"],
        )
        localized_keyword = {**strategy, "query": query}

        if name in chinese_names:
            assert localize_keyword_for_source(localized_keyword, "zh-CN")
            assert localize_keyword_for_source(localized_keyword, "en-US") is None
        else:
            assert localize_keyword_for_source(localized_keyword, "en-US")
            assert localize_keyword_for_source(localized_keyword, "zh-CN") is None

    database = Database(tmp_path / "tariff-keywords.db")
    database.initialize()
    seeded_names = {item["name"] for item in database.get_keywords()}
    assert chinese_names | english_names <= seeded_names


def test_core_and_recall_keyword_strategies_cover_each_report_category() -> None:
    focused = {
        item["name"]: item
        for item in DEFAULT_KEYWORDS
        if item.get("category_name") is not None
    }

    assert set(focused) == {
        "医疗手套政府采购与国产优先（中文）",
        "医疗手套政府采购与国产优先（英文）",
        "医疗手套关税调整精准版（中文）",
        "医疗手套关税调整精准版（英文）",
        "医疗手套召回与进口警示（中文）",
        "医疗手套召回与进口警示（英文）",
        "医疗手套贸易政策拓展召回（中文）",
        "医疗手套贸易政策拓展召回（英文）",
        "医疗手套关税调整拓展召回（中文）",
        "医疗手套关税调整拓展召回（英文）",
        "医疗手套行业法规拓展召回（中文）",
        "医疗手套行业法规拓展召回（英文）",
    }
    assert {item["category_name"] for item in focused.values()} == {
        "贸易政策",
        "关税调整",
        "行业法规",
    }
    for category_name in ("贸易政策", "关税调整", "行业法规"):
        category_strategies = [
            item
            for item in focused.values()
            if item["category_name"] == category_name
        ]
        recall_strategies = [
            item for item in category_strategies if "拓展召回" in item["name"]
        ]
        assert len(category_strategies) == 4
        assert len(recall_strategies) == 2
        assert any("（中文）" in item["name"] for item in recall_strategies)
        assert any("（英文）" in item["name"] for item in recall_strategies)
        assert all(item["require_local_match"] for item in recall_strategies)
        assert all(
            not item.get("require_local_match", False)
            for item in category_strategies
            if item not in recall_strategies
        )
    assert all(item["active"] for item in focused.values())
    assert all(item["lookback_days"] == 90 for item in focused.values())


def test_initialize_upgrades_the_original_generic_tariff_defaults(tmp_path) -> None:
    database = Database(tmp_path / "outdated-tariff-keywords.db")
    database.initialize()
    old_match_terms = [
        "加征关税",
        "关税调整",
        "进口关税",
        "对等关税",
        "进口附加费",
    ]
    old_context_terms = ["公告", "实施", "税率", "豁免", "暂停"]
    old_query = build_keyword_query(
        old_match_terms,
        context_terms=old_context_terms,
        lookback_days=30,
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE keywords
            SET query = ?, match_terms = ?, context_terms = ?
            WHERE name = '通用关税政策（中文）'
            """,
            (
                old_query,
                json.dumps(old_match_terms, ensure_ascii=False),
                json.dumps(old_context_terms, ensure_ascii=False),
            ),
        )

    database.initialize()

    keyword = next(
        item
        for item in database.get_keywords()
        if item["name"] == "通用关税政策（中文）"
    )
    assert keyword["match_terms"] == ["关税"]
    assert keyword["context_terms"] == [
        "政策",
        "调整",
        "加征",
        "豁免",
        "税率",
        "公告",
        "生效",
    ]

    custom_match_terms = ["自定义关税"]
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE keywords
            SET match_terms = ?
            WHERE name = '通用关税政策（中文）'
            """,
            (json.dumps(custom_match_terms, ensure_ascii=False),),
        )
    database.initialize()
    customized_keyword = next(
        item
        for item in database.get_keywords()
        if item["name"] == "通用关税政策（中文）"
    )
    assert customized_keyword["match_terms"] == custom_match_terms


def test_keyword_query_is_generated_only_from_match_terms() -> None:
    query = build_keyword_query(
        ["PE手套", "聚乙烯手套", "polyethylene gloves", "PE手套"]
    )

    assert query == (
        '("PE手套"OR"聚乙烯手套"OR"polyethylene gloves")'
    )
    assert "medical" not in query
    assert "-football" not in query
    assert " " not in query.replace("polyethylene gloves", "")


def test_keyword_query_combines_subjects_signals_exclusions_and_recency() -> None:
    query = build_keyword_query(
        ["nitrile gloves", "medical gloves", "NITRILE GLOVES"],
        context_terms=["tariff", "regulation", "Tariff"],
        exclude_terms=["boxing gloves", "football", "FOOTBALL"],
        lookback_days=30,
    )

    assert query == (
        '("nitrile gloves"OR"medical gloves")'
        'AND("tariff"OR"regulation")'
        '-"boxing gloves"-"football"when:30d'
    )


def test_initialize_migrates_keyword_categories_and_supports_assignment(tmp_path) -> None:
    database_path = tmp_path / "legacy-keywords.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                query TEXT NOT NULL,
                match_terms TEXT NOT NULL,
                context_terms TEXT NOT NULL DEFAULT '[]',
                exclude_terms TEXT NOT NULL DEFAULT '[]',
                lookback_days INTEGER NOT NULL DEFAULT 30,
                active INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO keywords
                (name, query, match_terms, created_at, updated_at)
            VALUES
                ('旧关键词', '\"tariff\"', '[\"tariff\"]',
                 '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z');
            """
        )

    database = Database(database_path)
    database.initialize()

    categories = database.get_keyword_categories()
    assert [item["name"] for item in categories] == [
        "贸易政策",
        "关税调整",
        "行业法规",
    ]
    legacy_keyword = next(
        item for item in database.get_keywords() if item["name"] == "旧关键词"
    )
    assert legacy_keyword["category_id"] is None
    assert legacy_keyword["category_name"] is None
    assert legacy_keyword["require_local_match"] == 0

    updated = database.update_keyword(
        legacy_keyword["id"],
        {
            "name": "旧关键词",
            "category_id": categories[1]["id"],
            "match_terms": ["tariff"],
            "context_terms": [],
            "exclude_terms": [],
            "lookback_days": 30,
            "active": True,
        },
    )

    assert updated
    assigned = next(
        item for item in database.get_keywords() if item["id"] == legacy_keyword["id"]
    )
    assert assigned["category_name"] == "关税调整"


def test_initialize_migrates_relevance_categories_and_seeds_prompt_settings(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-relevance.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE article_relevance_reviews (
                article_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                is_relevant INTEGER NOT NULL DEFAULT 0,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                relevance_reason TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '[]',
                confidence INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                raw_response TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reviewed_at TEXT,
                error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                categories TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, '2026-07-20T00:00:00Z')
            """,
            [
                ("ai_business_profile", LEGACY_DEFAULT_BUSINESS_PROFILE_V4),
                ("ai_relevance_prompt", LEGACY_DEFAULT_RELEVANCE_PROMPT_V4),
                ("ai_report_prompt", LEGACY_DEFAULT_REPORT_PROMPT_V4),
            ],
        )

    database = Database(database_path)
    database.initialize()

    with database.connect() as connection:
        review_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(article_relevance_reviews)"
            ).fetchall()
        }
        report_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(daily_reports)"
            ).fetchall()
        }
    settings = database.get_settings()

    assert {
        "category",
        "secondary_categories",
        "keyword_categories",
    } <= review_columns
    assert {"keyword_category_id", "keyword_category_name"} <= report_columns
    assert settings["ai_relevance_prompt"] == DEFAULT_RELEVANCE_PROMPT
    assert settings["ai_business_profile"] == DEFAULT_BUSINESS_PROFILE
    assert settings["ai_report_prompt"] == DEFAULT_REPORT_PROMPT
    assert {
        category_name: settings[REPORT_CATEGORY_SETTING_KEYS[category_name]]
        for category_name in DEFAULT_REPORT_CATEGORY_PROMPTS
    } == DEFAULT_REPORT_CATEGORY_PROMPTS


def test_keyword_hit_stats_count_distinct_relevant_articles_by_category(
    tmp_path,
) -> None:
    database = Database(tmp_path / "keyword-hit-stats.db")
    database.initialize()
    categories = {
        item["name"]: item["id"] for item in database.get_keyword_categories()
    }
    with database.connect() as connection:
        connection.execute("UPDATE keywords SET archived = 1")

    policy_keyword_id = database.create_keyword(
        {
            "name": "测试贸易政策",
            "category_id": categories["贸易政策"],
            "match_terms": ["tariff"],
            "active": True,
        }
    )
    remedy_keyword_id = database.create_keyword(
        {
            "name": "测试贸易救济",
            "category_id": categories["贸易政策"],
            "match_terms": ["anti-dumping"],
            "active": True,
        }
    )
    regulation_keyword_id = database.create_keyword(
        {
            "name": "测试法规",
            "category_id": categories["行业法规"],
            "match_terms": ["medical regulation"],
            "active": True,
        }
    )
    inactive_keyword_id = database.create_keyword(
        {
            "name": "停用的历史贸易政策",
            "category_id": categories["贸易政策"],
            "match_terms": ["legacy tariff"],
            "active": False,
        }
    )

    now = "2026-07-25T00:00:00Z"
    with database.connect() as connection:
        article_ids = []
        for index in range(1, 5):
            url = f"https://example.com/hit-{index}"
            cursor = connection.execute(
                """
                INSERT INTO articles
                    (title, url, canonical_url, fingerprint, published_at,
                     collected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"Article {index}", url, url, f"hit-{index}", now, now),
            )
            article_ids.append(int(cursor.lastrowid))

        connection.executemany(
            """
            INSERT INTO article_keywords
                (article_id, keyword_id, matched_terms)
            VALUES (?, ?, '[]')
            """,
            [
                (article_ids[0], policy_keyword_id),
                (article_ids[0], remedy_keyword_id),
                (article_ids[1], policy_keyword_id),
                (article_ids[2], remedy_keyword_id),
                (article_ids[3], regulation_keyword_id),
                (article_ids[3], inactive_keyword_id),
            ],
        )
        connection.executemany(
            """
            INSERT INTO article_relevance_reviews
                (article_id, status, is_relevant, category,
                 secondary_categories, reviewed_at)
            VALUES (?, 'success', ?, ?, '[]', ?)
            """,
            [
                (article_ids[0], 1, "policy_regulation", now),
                (article_ids[1], 0, "other", now),
                (article_ids[2], 1, "market_demand", now),
                (article_ids[3], 1, "policy_regulation", now),
            ],
        )

    stats = database.keyword_hit_stats()
    policy_stats = next(
        item
        for item in stats["categories"]
        if item["category_name"] == "贸易政策"
    )
    policy_keyword_stats = next(
        item
        for item in stats["keywords"]
        if item["keyword_id"] == policy_keyword_id
    )
    remedy_keyword_stats = next(
        item
        for item in stats["keywords"]
        if item["keyword_id"] == remedy_keyword_id
    )
    inactive_keyword_stats = next(
        item
        for item in stats["keywords"]
        if item["keyword_id"] == inactive_keyword_id
    )

    assert policy_stats["keyword_count"] == 2
    assert policy_stats["hit_count"] == 3
    assert policy_stats["reviewed_count"] == 3
    assert policy_stats["business_relevant_count"] == 2
    assert policy_stats["relevant_count"] == 1
    assert policy_stats["pending_review_count"] == 0
    assert policy_stats["hit_rate"] == pytest.approx(1 / 3)
    assert policy_keyword_stats["hit_count"] == 2
    assert policy_keyword_stats["relevant_count"] == 1
    assert policy_keyword_stats["hit_rate"] == 0.5
    assert remedy_keyword_stats["hit_count"] == 2
    assert remedy_keyword_stats["reviewed_count"] == 2
    assert remedy_keyword_stats["business_relevant_count"] == 2
    assert remedy_keyword_stats["relevant_count"] == 1
    assert remedy_keyword_stats["pending_review_count"] == 0
    assert remedy_keyword_stats["hit_rate"] == 0.5
    assert inactive_keyword_stats["active"] == 0
    assert inactive_keyword_stats["hit_count"] == 1
    assert inactive_keyword_stats["relevant_count"] == 1
    assert stats["overall"]["keyword_count"] == 3
    assert stats["overall"]["hit_count"] == 4
    assert stats["overall"]["reviewed_count"] == 4
    assert stats["overall"]["business_relevant_count"] == 3
    assert stats["overall"]["relevant_count"] == 2
    assert stats["overall"]["pending_review_count"] == 0
    assert stats["overall"]["hit_rate"] == 0.5


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


def test_search_source_can_skip_local_filter_but_direct_source_still_checks(
    tmp_path,
) -> None:
    database = Database(tmp_path / "search-local-filter.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": "测试搜索源",
            "url_template": "https://search.example/rss?q={query}",
            "mode": "search",
            "language": "zh-CN",
            "country": "CN",
            "active": True,
        }
    )
    database.create_source(
        {
            "name": "测试直连源",
            "url_template": "https://direct.example/feed.xml",
            "mode": "direct",
            "language": "zh-CN",
            "country": "CN",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "手套贸易",
            "match_terms": ["手套"],
            "context_terms": ["贸易"],
            "lookback_days": 30,
            "active": True,
        }
    )
    database.set_setting("search_local_keyword_filter", "false")
    rss_without_subject = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Search result</title><item>
      <title>New international trade policy announced</title>
      <link>https://example.com/trade-policy</link>
      <source>Example News</source>
      <guid>trade-policy-1</guid>
      <pubDate>Mon, 20 Jul 2026 02:00:00 GMT</pubDate>
      <description>Export rules were updated.</description>
    </item></channel></rss>"""
    collector = Collector(
        database,
        feed_fetcher=lambda _url, _timeout: rss_without_subject,
    )
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-06-22T00:00:00Z"
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    details = {item["source_name"]: item for item in result["details"]}
    assert details["测试搜索源"]["items_matched"] == 1
    assert details["测试直连源"]["items_matched"] == 0
    assert result["items_inserted"] == 1
    assert database.article_count() == 1


def test_keyword_can_require_local_match_when_global_filter_is_disabled(
    tmp_path,
) -> None:
    database = Database(tmp_path / "keyword-local-filter.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE rss_sources SET active = 0, archived = 1")
        connection.execute("UPDATE keywords SET active = 0, archived = 1")
    database.create_source(
        {
            "name": "测试搜索源",
            "url_template": "https://search.example/rss?q={query}",
            "mode": "search",
            "language": "en-US",
            "country": "US",
            "active": True,
        }
    )
    database.create_keyword(
        {
            "name": "Glove trade recall",
            "match_terms": ["glove industry"],
            "context_terms": ["trade restriction"],
            "lookback_days": 30,
            "require_local_match": True,
            "active": True,
        }
    )
    database.set_setting("search_local_keyword_filter", "false")
    rss_without_subject = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Search result</title><item>
      <title>New international trade policy announced</title>
      <link>https://example.com/trade-policy</link>
      <source>Example News</source>
      <guid>trade-policy-2</guid>
      <pubDate>Mon, 20 Jul 2026 02:00:00 GMT</pubDate>
      <description>Export rules were updated.</description>
    </item></channel></rss>"""
    collector = Collector(
        database,
        feed_fetcher=lambda _url, _timeout: rss_without_subject,
    )
    started = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    run_id = database.create_run(
        "manual", started.isoformat(), "2026-06-22T00:00:00Z"
    )

    collector.collect(run_id, started)

    result = database.get_run(run_id)
    assert result is not None
    assert result["items_matched"] == 0
    assert result["items_inserted"] == 0
    assert database.article_count() == 0


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
            "require_local_match": True,
            "active": True,
        }
    )

    keyword = next(item for item in database.get_keywords() if item["id"] == keyword_id)

    assert keyword["context_terms"] == ["tariff", "demand"]
    assert keyword["exclude_terms"] == ["boxing", "football"]
    assert keyword["lookback_days"] == 14
    assert keyword["require_local_match"] == 1
    assert keyword["query"] == (
        '("nitrile gloves"OR"medical gloves")'
        'AND("tariff"OR"demand")-"boxing"-"football"when:14d'
    )
    feed_url = build_feed_url(
        {"mode": "search", "url_template": "https://example.com/rss?q={query}"},
        keyword,
    )
    assert "{query}" not in feed_url
    assert "when%3A14d" in feed_url
    assert "-%22boxing%22" in feed_url


def test_keyword_query_allows_long_google_expression() -> None:
    query = build_keyword_query(
        [f"very specific medical product phrase {index}" for index in range(20)],
        context_terms=[f"business signal phrase {index}" for index in range(20)],
        lookback_days=30,
    )

    assert len(query) > 200
    assert query.endswith("when:30d")


def test_updating_keyword_strategy_clears_stale_hits_and_cursor(tmp_path) -> None:
    database = Database(tmp_path / "keyword-strategy-reset.db")
    database.initialize()
    keyword_id = database.create_keyword(
        {
            "name": "Recall strategy",
            "match_terms": ["glove industry"],
            "context_terms": ["trade restriction"],
            "lookback_days": 30,
            "active": True,
        }
    )
    with database.connect() as connection:
        source_id = connection.execute(
            "SELECT id FROM rss_sources ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        article_id = connection.execute(
            """
            INSERT INTO articles
                (title, url, canonical_url, fingerprint, published_at, collected_at)
            VALUES
                ('Old candidate', 'https://example.com/old',
                 'https://example.com/old', 'old-candidate',
                 '2026-07-20T00:00:00Z', '2026-07-20T01:00:00Z')
            """
        ).lastrowid
        connection.execute(
            """
            INSERT INTO article_keywords (article_id, keyword_id, matched_terms)
            VALUES (?, ?, '["glove industry"]')
            """,
            (article_id, keyword_id),
        )
        connection.execute(
            """
            INSERT INTO collection_cursors
                (rss_source_id, keyword_id, last_collected_at)
            VALUES (?, ?, '2026-07-20T01:00:00Z')
            """,
            (source_id, keyword_id),
        )

    assert database.update_keyword(
        keyword_id,
        {
            "name": "Recall strategy",
            "match_terms": ["rubber gloves"],
            "context_terms": ["trade restriction"],
            "lookback_days": 30,
            "require_local_match": True,
            "active": True,
        },
    )

    with database.connect() as connection:
        hit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM article_keywords WHERE keyword_id = ?",
            (keyword_id,),
        ).fetchone()["count"]
        cursor_count = connection.execute(
            "SELECT COUNT(*) AS count FROM collection_cursors WHERE keyword_id = ?",
            (keyword_id,),
        ).fetchone()["count"]
    assert hit_count == 0
    assert cursor_count == 0


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


def test_second_run_rechecks_lookback_when_incremental_collection_is_off(
    tmp_path,
) -> None:
    database = configured_database(tmp_path)
    database.set_setting("incremental_collection", "false")
    collector = Collector(database, feed_fetcher=lambda _url, _timeout: RSS_XML)
    first_started = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
    first_run = database.create_run(
        "manual", first_started.isoformat(), "2026-06-20T16:00:00Z"
    )
    collector.collect(first_run, first_started)

    second_started = first_started + timedelta(hours=1)
    second_run = database.create_run(
        "manual", second_started.isoformat(), "2026-06-20T16:00:00Z"
    )
    collector.collect(second_run, second_started)

    second_result = database.get_run(second_run)
    assert second_result is not None
    assert second_result["items_inserted"] == 0
    assert second_result["items_matched"] == 1
    assert second_result["duplicates"] == 1
    assert second_result["details"][0]["skipped_outside_window"] == 0
    assert second_result["details"][0]["window_start"] == "2026-06-20T16:00:00Z"
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
            """
            SELECT country, site_domain, crawler_enabled
            FROM rss_sources
            WHERE id = 1
            """
        ).fetchone()
        article = connection.execute(
            "SELECT publisher_normalized FROM articles WHERE id = 1"
        ).fetchone()
        provenance = connection.execute(
            "SELECT language, country FROM article_sources WHERE article_id = 1"
        ).fetchone()
    assert dict(source) == {
        "country": "US",
        "site_domain": "",
        "crawler_enabled": 0,
    }
    assert article["publisher_normalized"] == "Example News"
    assert dict(provenance) == {"language": "en-US", "country": "US"}
