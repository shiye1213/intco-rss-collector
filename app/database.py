from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .mysql_backend import (
    DatabaseBackendError,
    DatabaseIntegrityError,
    MySQLConnection,
    dump_mysql_database,
    parse_mysql_url,
)
from .normalization import infer_country, normalize_publisher
from .prompts import (
    DEFAULT_BUSINESS_PROFILE,
    DEFAULT_RELEVANCE_PROMPT,
    DEFAULT_REPORT_PROMPT,
    LEGACY_DEFAULT_BUSINESS_PROFILE,
    LEGACY_DEFAULT_BUSINESS_PROFILE_V4,
    LEGACY_DEFAULT_BUSINESS_PROFILE_V5,
    LEGACY_DEFAULT_RELEVANCE_PROMPT_V4,
    LEGACY_DEFAULT_RELEVANCE_PROMPT_V5,
    LEGACY_DEFAULT_RELEVANCE_PROMPT_V6,
    LEGACY_DEFAULT_RELEVANCE_PROMPT_V7,
    LEGACY_DEFAULT_REPORT_PROMPT,
    LEGACY_DEFAULT_REPORT_PROMPT_V4,
    LEGACY_DEFAULT_REPORT_PROMPT_V5,
    LEGACY_DEFAULT_REPORT_PROMPT_V6,
    LEGACY_DEFAULT_REPORT_PROMPT_V7,
    LEGACY_DEFAULT_REPORT_PROMPT_V8,
    LEGACY_DEFAULT_REPORT_PROMPT_V9,
)
from .query_builder import build_keyword_query


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rss_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url_template TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('search', 'direct')),
    language TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    site_domain TEXT NOT NULL DEFAULT '',
    crawler_enabled INTEGER NOT NULL DEFAULT 0,
    crawler_failure_kind TEXT NOT NULL DEFAULT '',
    crawler_failure_count INTEGER NOT NULL DEFAULT 0,
    crawler_cooldown_until TEXT,
    crawler_last_error TEXT NOT NULL DEFAULT '',
    crawler_last_success_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keyword_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES keyword_categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL UNIQUE,
    query TEXT NOT NULL,
    match_terms TEXT NOT NULL,
    context_terms TEXT NOT NULL DEFAULT '[]',
    exclude_terms TEXT NOT NULL DEFAULT '[]',
    lookback_days INTEGER NOT NULL DEFAULT 30,
    require_local_match INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    publisher TEXT NOT NULL DEFAULT '',
    publisher_normalized TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    rss_source_id INTEGER REFERENCES rss_sources(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_canonical_url
ON articles(canonical_url) WHERE canonical_url IS NOT NULL AND canonical_url <> '';
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_collected_at ON articles(collected_at DESC);

CREATE TABLE IF NOT EXISTS article_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    rss_source_id INTEGER NOT NULL REFERENCES rss_sources(id) ON DELETE CASCADE,
    feed_url TEXT NOT NULL DEFAULT '',
    observed_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    guid TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(article_id, rss_source_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_article_sources_article_id
ON article_sources(article_id);
CREATE INDEX IF NOT EXISTS idx_article_sources_source_id
ON article_sources(rss_source_id);

CREATE TABLE IF NOT EXISTS article_keywords (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    matched_terms TEXT NOT NULL,
    PRIMARY KEY (article_id, keyword_id)
);

CREATE TABLE IF NOT EXISTS collection_cursors (
    rss_source_id INTEGER NOT NULL REFERENCES rss_sources(id) ON DELETE CASCADE,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    last_collected_at TEXT NOT NULL,
    PRIMARY KEY (rss_source_id, keyword_id)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'interrupted')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    tasks_total INTEGER NOT NULL DEFAULT 0,
    tasks_succeeded INTEGER NOT NULL DEFAULT 0,
    tasks_failed INTEGER NOT NULL DEFAULT 0,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_matched INTEGER NOT NULL DEFAULT 0,
    items_inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_started_at
ON collection_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS collection_run_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    rss_source_id INTEGER NOT NULL REFERENCES rss_sources(id) ON DELETE CASCADE,
    keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_matched INTEGER NOT NULL DEFAULT 0,
    items_inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    skipped_outside_window INTEGER NOT NULL DEFAULT 0,
    skipped_without_date INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_collection_run_details_run_id
ON collection_run_details(run_id);

CREATE TABLE IF NOT EXISTS ai_analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'collection')),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed', 'interrupted')),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    articles_total INTEGER NOT NULL DEFAULT 0,
    articles_succeeded INTEGER NOT NULL DEFAULT 0,
    articles_failed INTEGER NOT NULL DEFAULT 0,
    relevant_count INTEGER NOT NULL DEFAULT 0,
    irrelevant_count INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_runs_started_at
ON ai_analysis_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS article_analyses (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('processing', 'success', 'failed')),
    is_relevant INTEGER NOT NULL DEFAULT 0,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_reason TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    secondary_categories TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    impact_direction TEXT NOT NULL DEFAULT 'neutral',
    impact_score INTEGER NOT NULL DEFAULT 1,
    impact_analysis TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'low',
    risk_score INTEGER NOT NULL DEFAULT 0,
    risk_factors TEXT NOT NULL DEFAULT '[]',
    opportunities TEXT NOT NULL DEFAULT '[]',
    recommended_actions TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    confidence INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    analyzed_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_article_analyses_relevance
ON article_analyses(status, is_relevant, category);

CREATE TABLE IF NOT EXISTS article_contents (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('processing', 'success', 'failed')),
    requested_url TEXT NOT NULL DEFAULT '',
    final_url TEXT NOT NULL DEFAULT '',
    full_text TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    content_chars INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER NOT NULL DEFAULT 0,
    content_type TEXT NOT NULL DEFAULT '',
    extractor TEXT NOT NULL DEFAULT '',
    fetched_at TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    failure_kind TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    ignored_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_article_contents_status
ON article_contents(status, fetched_at);

CREATE TABLE IF NOT EXISTS article_relevance_reviews (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('processing', 'success', 'failed')),
    is_relevant INTEGER NOT NULL DEFAULT 0,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_reason TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    secondary_categories TEXT NOT NULL DEFAULT '[]',
    keyword_categories TEXT NOT NULL DEFAULT '[]',
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

CREATE INDEX IF NOT EXISTS idx_article_relevance_reviews_result
ON article_relevance_reviews(status, is_relevant, relevance_score);

CREATE TABLE IF NOT EXISTS business_articles (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    analysis_status TEXT NOT NULL CHECK (analysis_status IN ('processing', 'success', 'failed')),
    relevance_score INTEGER NOT NULL,
    relevance_reason TEXT NOT NULL,
    relevance_confidence INTEGER NOT NULL DEFAULT 0,
    relevance_evidence TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT 'other',
    secondary_categories TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    impact_direction TEXT NOT NULL DEFAULT 'neutral',
    impact_score INTEGER NOT NULL DEFAULT 1,
    impact_analysis TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'low',
    risk_score INTEGER NOT NULL DEFAULT 0,
    risk_factors TEXT NOT NULL DEFAULT '[]',
    opportunities TEXT NOT NULL DEFAULT '[]',
    recommended_actions TEXT NOT NULL DEFAULT '[]',
    analysis_evidence TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    accepted_at TEXT NOT NULL,
    analyzed_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_business_articles_analysis
ON business_articles(analysis_status, category, risk_score DESC);

CREATE TABLE IF NOT EXISTS ai_analysis_run_items (
    run_id INTEGER NOT NULL REFERENCES ai_analysis_runs(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'success', 'failed')),
    is_relevant INTEGER NOT NULL DEFAULT 0,
    content_status TEXT NOT NULL DEFAULT 'pending',
    relevance_status TEXT NOT NULL DEFAULT 'pending',
    business_analysis_status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, article_id)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    categories TEXT NOT NULL DEFAULT '[]',
    keyword_category_id INTEGER REFERENCES keyword_categories(id) ON DELETE SET NULL,
    keyword_category_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'interrupted')),
    risk_level TEXT NOT NULL DEFAULT 'low',
    risk_score INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]',
    executive_summary TEXT NOT NULL DEFAULT '',
    risk_basis TEXT NOT NULL DEFAULT '',
    key_developments TEXT NOT NULL DEFAULT '[]',
    key_risks TEXT NOT NULL DEFAULT '[]',
    opportunities TEXT NOT NULL DEFAULT '[]',
    recommended_actions TEXT NOT NULL DEFAULT '[]',
    watchlist TEXT NOT NULL DEFAULT '[]',
    article_count INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    raw_response TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_daily_reports_date
ON daily_reports(report_date DESC, id DESC);

CREATE TABLE IF NOT EXISTS daily_report_articles (
    report_id INTEGER NOT NULL REFERENCES daily_reports(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, article_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


DEFAULT_KEYWORD_CATEGORIES = (
    "贸易政策",
    "关税调整",
    "行业法规",
)


DEFAULT_SOURCES = (
    {
        "name": "Google News 中文",
        "url_template": (
            "https://news.google.com/rss/search?q={query}"
            "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        ),
        "mode": "search",
        "language": "zh-CN",
        "country": "CN",
        "active": True,
    },
    {
        "name": "Google News 英文",
        "url_template": (
            "https://news.google.com/rss/search?q={query}"
            "&hl=en-US&gl=US&ceid=US:en"
        ),
        "mode": "search",
        "language": "en-US",
        "country": "US",
        "active": True,
    },
    {
        "name": "Google News 马来西亚英文",
        "url_template": (
            "https://news.google.com/rss/search?q={query}"
            "&hl=en-MY&gl=MY&ceid=MY:en"
        ),
        "mode": "search",
        "language": "en-MY",
        "country": "MY",
        "active": True,
    },
    {
        "name": "Google News 马来西亚 The Star",
        "url_template": (
            "https://news.google.com/rss/search?"
            "q=site%3Athestar.com.my+{query}"
            "&hl=en-MY&gl=MY&ceid=MY:en"
        ),
        "mode": "search",
        "language": "en-MY",
        "country": "MY",
        "active": True,
    },
    {
        "name": "Google News 马来西亚 The Edge",
        "url_template": (
            "https://news.google.com/rss/search?"
            "q=site%3Atheedgemalaysia.com+{query}"
            "&hl=en-MY&gl=MY&ceid=MY:en"
        ),
        "mode": "search",
        "language": "en-MY",
        "country": "MY",
        "active": True,
    },
    {
        "name": "欧盟医疗器械标准",
        "url_template": "https://ec.europa.eu/newsroom/growth/feed?tpa_id=30111",
        "mode": "direct",
        "language": "en",
        "country": "EU",
        "active": True,
    },
    {
        "name": "英国 MHRA",
        "url_template": (
            "https://www.gov.uk/government/organisations/"
            "medicines-and-healthcare-products-regulatory-agency.atom"
        ),
        "mode": "direct",
        "language": "en",
        "country": "GB",
        "active": True,
    },
    {
        "name": "巴西 ANVISA",
        "url_template": "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/RSS",
        "mode": "direct",
        "language": "pt-BR",
        "country": "BR",
        "active": True,
    },
    {
        "name": "加拿大医疗器械召回",
        "url_template": (
            "https://recalls-rappels.canada.ca/en/feed/"
            "medical-devices-alerts-recalls"
        ),
        "mode": "direct",
        "language": "en",
        "country": "CA",
        "active": True,
    },
    {
        "name": "ECDC 传染病威胁",
        "url_template": "https://www.ecdc.europa.eu/en/taxonomy/term/1505/feed",
        "mode": "direct",
        "language": "en",
        "country": "EU",
        "active": True,
    },
    {
        "name": "欧盟贸易新闻",
        "url_template": "https://policy.trade.ec.europa.eu/node/2/rss_en",
        "mode": "direct",
        "language": "en",
        "country": "EU",
        "active": True,
    },
    {
        "name": "WTO 新闻",
        "url_template": "https://www.wto.org/library/rss/latest_news_e.xml",
        "mode": "direct",
        "language": "en",
        "country": "GLOBAL",
        "active": True,
    },
)

DEFAULT_KEYWORDS = (
    {
        "name": "医疗手套政府采购与国产优先（中文）",
        "category_name": "贸易政策",
        "match_terms": ["丁腈手套", "医用手套", "一次性手套", "防护手套"],
        "context_terms": [
            "政府采购",
            "公共采购",
            "国产优先",
            "本国产品",
            "本土化采购",
            "本地化要求",
            "国产化要求",
        ],
        "exclude_terms": ["关税", "进口税", "拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套政府采购与国产优先（英文）",
        "category_name": "贸易政策",
        "match_terms": ["nitrile gloves", "medical gloves"],
        "context_terms": [
            "government procurement",
            "public procurement",
            "federal procurement",
            "domestic preference",
            "Buy American",
            "local content requirement",
        ],
        "exclude_terms": ["tariff", "duty", "boxing", "football", "baseball"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套关税调整精准版（中文）",
        "category_name": "关税调整",
        "match_terms": ["丁腈手套", "医用手套", "一次性手套", "PVC手套"],
        "context_terms": [
            "关税",
            "进口关税",
            "进口税",
            "加征关税",
            "关税税率",
            "关税豁免",
            "反倾销税",
            "反补贴税",
        ],
        "exclude_terms": ["拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套关税调整精准版（英文）",
        "category_name": "关税调整",
        "match_terms": ["nitrile gloves", "medical gloves", "vinyl gloves"],
        "context_terms": [
            "tariff",
            "import duty",
            "additional duty",
            "glove tariff",
            "Section 301",
            "tariff exemption",
            "anti-dumping duty",
            "countervailing duty",
        ],
        "exclude_terms": ["boxing", "football", "baseball"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套召回与进口警示（中文）",
        "category_name": "行业法规",
        "match_terms": ["丁腈手套", "医用手套", "一次性手套", "防护手套"],
        "context_terms": [
            "召回",
            "产品召回",
            "进口警示",
            "质量通报",
            "医疗器械注册",
            "产品认证",
            "强制标准",
        ],
        "exclude_terms": ["关税", "进口税", "拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套召回与进口警示（英文）",
        "category_name": "行业法规",
        "match_terms": ["nitrile gloves", "medical gloves", "disposable gloves"],
        "context_terms": [
            "recall",
            "import alert",
            "FDA import alert",
            "510(k)",
            "medical device regulation",
            "product registration",
        ],
        "exclude_terms": ["tariff", "duty", "boxing", "football", "baseball"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "医疗手套贸易政策拓展召回（中文）",
        "category_name": "贸易政策",
        "match_terms": ["橡胶手套", "手套行业", "手套企业", "手套厂商"],
        "context_terms": [
            "进口禁令",
            "出口禁令",
            "贸易限制",
            "原产地规则",
            "本地化要求",
        ],
        "exclude_terms": ["关税", "进口税", "拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套贸易政策拓展召回（英文）",
        "category_name": "贸易政策",
        "match_terms": [
            "rubber gloves",
            "disposable gloves",
            "glove industry",
            "glove makers",
        ],
        "context_terms": [
            "import ban",
            "export ban",
            "trade restriction",
            "rules of origin",
        ],
        "exclude_terms": ["tariff", "duty", "boxing"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套关税调整拓展召回（中文）",
        "category_name": "关税调整",
        "match_terms": ["橡胶手套", "手套行业", "手套企业", "手套厂商"],
        "context_terms": [
            "关税上调",
            "关税豁免",
            "反倾销税",
            "反补贴税",
            "贸易救济",
        ],
        "exclude_terms": ["拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套关税调整拓展召回（英文）",
        "category_name": "关税调整",
        "match_terms": [
            "rubber gloves",
            "disposable gloves",
            "glove sector",
            "glove makers",
        ],
        "context_terms": [
            "tariff increase",
            "tariff exemption",
            "anti-dumping duty",
            "countervailing duty",
        ],
        "exclude_terms": ["boxing", "football", "baseball"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套行业法规拓展召回（中文）",
        "category_name": "行业法规",
        "match_terms": ["橡胶手套", "手套行业", "手套企业", "手套厂商"],
        "context_terms": [
            "手套召回",
            "进口警示",
            "产品认证",
            "注册要求",
            "医疗器械法规",
        ],
        "exclude_terms": ["关税", "进口税", "拳击", "橄榄球", "棒球"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套行业法规拓展召回（英文）",
        "category_name": "行业法规",
        "match_terms": [
            "rubber gloves",
            "disposable gloves",
            "glove industry",
            "glove makers",
        ],
        "context_terms": [
            "FDA",
            "glove recall",
            "import alert",
            "medical device rule",
        ],
        "exclude_terms": ["tariff", "duty", "boxing"],
        "lookback_days": 90,
        "require_local_match": True,
        "active": True,
    },
    {
        "name": "医疗手套供需与价格（英文）",
        "match_terms": [
            "nitrile gloves",
            "medical gloves",
            "disposable gloves",
        ],
        "context_terms": [
            "price",
            "demand",
            "orders",
            "tender",
            "procurement",
            "capacity",
            "utilization",
            "average selling price",
            "shortage",
        ],
        "exclude_terms": ["boxing", "football", "baseball"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "医疗手套供需与价格（中文）",
        "match_terms": ["丁腈手套", "医用手套", "一次性手套"],
        "context_terms": [
            "价格",
            "需求",
            "订单",
            "招标",
            "采购",
            "产能",
            "开工率",
            "平均售价",
            "短缺",
        ],
        "exclude_terms": ["拳击", "橄榄球", "棒球"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "医疗手套贸易与法规",
        "match_terms": [
            "丁腈手套",
            "医用手套",
            "nitrile gloves",
            "medical gloves",
        ],
        "context_terms": [
            "关税",
            "法规",
            "进口",
            "召回",
            "tariff",
            "regulation",
            "import",
            "recall",
        ],
        "exclude_terms": ["boxing", "football", "baseball"],
        "lookback_days": 14,
        "active": False,
    },
    {
        "name": "PE 与 PVC 手套（英文）",
        "match_terms": [
            "polyethylene gloves",
            "vinyl gloves",
            "disposable PE gloves",
        ],
        "context_terms": [
            "price",
            "demand",
            "procurement",
            "shortage",
        ],
        "exclude_terms": ["football", "NFL", "player exclusive", "boxing"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "PE 与 PVC 手套（中文）",
        "match_terms": ["PE手套", "聚乙烯手套", "PVC手套"],
        "context_terms": ["价格", "需求", "采购", "短缺", "法规"],
        "exclude_terms": ["橄榄球", "足球", "拳击"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "马来西亚手套竞争对手",
        "match_terms": [
            "Top Glove",
            "Hartalega",
            "Kossan",
            "Supermax",
            "Sri Trang",
            "Careplus",
            "Riverstone",
        ],
        "context_terms": [
            "glove",
            "capacity",
            "expansion",
            "factory",
            "price",
            "earnings",
            "utilization",
            "average selling price",
            "margin",
        ],
        "exclude_terms": ["NBA", "movie", "prison"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "Medline 运营动态",
        "match_terms": ["Medline"],
        "context_terms": [
            "glove",
            "medical supplies",
            "capacity",
            "FDA",
            "recall",
            "distribution",
            "warehouse",
        ],
        "exclude_terms": [
            "shares",
            "holdings",
            "stake",
            "securities",
            "investors",
        ],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "国内手套企业动态",
        "match_terms": ["蓝帆医疗", "中红医疗"],
        "context_terms": [
            "产能",
            "报价",
            "订单",
            "业绩",
            "工厂",
            "扩产",
            "召回",
        ],
        "exclude_terms": [],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "手套原材料",
        "match_terms": [
            "NBR latex",
            "nitrile butadiene rubber",
            "PVC resin",
            "acrylonitrile",
            "butadiene",
            "丁腈胶乳",
            "PVC树脂",
            "丙烯腈",
            "丁二烯",
        ],
        "context_terms": [
            "价格",
            "供应",
            "短缺",
            "price",
            "supply",
            "shortage",
            "export",
        ],
        "exclude_terms": ["training camp", "obituary"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "国际航运与物流（英文）",
        "match_terms": [
            "container freight",
            "shipping rates",
            "Red Sea shipping",
        ],
        "context_terms": [
            "medical supplies",
            "gloves",
            "export manufacturing",
        ],
        "exclude_terms": ["cruise", "travel deal"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "国际航运与物流（中文）",
        "match_terms": ["集装箱运价", "海运费", "红海航运"],
        "context_terms": ["医疗用品", "手套", "出口制造", "供应链"],
        "exclude_terms": ["邮轮", "旅游"],
        "lookback_days": 14,
        "active": True,
    },
    {
        "name": "英科医疗公司动态",
        "match_terms": ["英科医疗", "INTCO Medical"],
        "context_terms": [
            "公告",
            "业绩",
            "订单",
            "产能",
            "工厂",
            "扩产",
            "召回",
            "关税",
            "earnings",
            "orders",
            "capacity",
            "factory",
            "recall",
            "tariff",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "康复护理产品（中文）",
        "match_terms": ["轮椅", "电动代步车", "助行器"],
        "context_terms": ["采购", "需求", "召回", "认证", "法规", "市场"],
        "exclude_terms": ["轮椅篮球", "残奥会"],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "康复护理产品（英文）",
        "match_terms": ["wheelchair", "mobility scooter", "walking aid"],
        "context_terms": [
            "procurement",
            "demand",
            "recall",
            "FDA",
            "regulation",
            "market",
        ],
        "exclude_terms": ["wheelchair basketball", "Paralympics"],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "理疗与护理产品（中文）",
        "match_terms": ["冷热敷产品", "冷敷袋", "热敷袋", "急救包"],
        "context_terms": ["医疗", "护理", "召回", "认证", "需求", "采购"],
        "exclude_terms": ["食品保温", "餐饮"],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "理疗与护理产品（英文）",
        "match_terms": [
            "hot cold pack",
            "cold therapy pack",
            "instant cold pack",
            "first aid kit",
        ],
        "context_terms": [
            "medical",
            "healthcare",
            "recall",
            "FDA",
            "demand",
            "procurement",
        ],
        "exclude_terms": ["food delivery", "meal kit"],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "公共卫生与防护需求（中文）",
        "match_terms": [
            "传染病暴发",
            "疫情暴发",
            "公共卫生紧急事件",
            "医院感染",
        ],
        "context_terms": [
            "医用手套",
            "个人防护装备",
            "PPE",
            "防护用品",
            "医疗物资",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "公共卫生与防护需求（英文）",
        "match_terms": [
            "disease outbreak",
            "public health emergency",
            "hospital infection",
            "health emergency",
        ],
        "context_terms": [
            "medical gloves",
            "personal protective equipment",
            "PPE",
            "medical supplies",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": True,
    },
    {
        "name": "医疗手套关税（中文）",
        "match_terms": ["丁腈手套", "医用手套", "一次性手套"],
        "context_terms": ["关税"],
        "exclude_terms": ["拳击", "足球"],
        "lookback_days": 365,
        "active": False,
    },
    {
        "name": "医疗手套关税（英文）",
        "match_terms": [
            "medical gloves",
            "nitrile gloves",
            "surgical gloves",
        ],
        "context_terms": ["tariff", "import duty"],
        "exclude_terms": ["boxing", "football", "baseball"],
        "lookback_days": 365,
        "active": False,
    },
    {
        "name": "美国关税法律工具（中文）",
        "match_terms": [
            "301条款",
            "232条款",
            "122条款",
            "对等关税",
            "进口附加费",
        ],
        "context_terms": ["加征", "调整", "豁免", "暂停", "生效"],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
    {
        "name": "美国关税法律工具（英文）",
        "match_terms": [
            "Section 301",
            "Section 232",
            "Section 122",
            "reciprocal tariff",
        ],
        "context_terms": [
            "tariff",
            "duty",
            "exclusion",
            "proclamation",
            "executive order",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
    {
        "name": "贸易救济案件（中文）",
        "match_terms": [
            "反倾销",
            "反补贴",
            "保障措施",
            "贸易救济",
        ],
        "context_terms": [
            "立案调查",
            "初裁",
            "终裁",
            "行政复审",
            "日落复审",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
    {
        "name": "贸易救济案件（英文）",
        "match_terms": [
            "anti-dumping",
            "countervailing",
            "safeguard",
            "trade remedy",
        ],
        "context_terms": [
            "investigation",
            "preliminary determination",
            "final determination",
            "review",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
    {
        "name": "手套原材料关税（中文）",
        "match_terms": ["丁腈胶乳", "PVC树脂", "合成橡胶"],
        "context_terms": ["关税", "反倾销", "反补贴", "进口税"],
        "exclude_terms": ["轮胎", "汽车"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "手套原材料关税（英文）",
        "match_terms": ["NBR latex", "PVC resin", "synthetic rubber"],
        "context_terms": [
            "tariff",
            "anti-dumping",
            "countervailing duty",
            "import duty",
        ],
        "exclude_terms": ["tire", "tyre", "automotive"],
        "lookback_days": 90,
        "active": True,
    },
    {
        "name": "通用关税政策（中文）",
        "match_terms": ["关税"],
        "context_terms": [
            "政策",
            "调整",
            "加征",
            "豁免",
            "税率",
            "公告",
            "生效",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
    {
        "name": "通用关税政策（英文）",
        "match_terms": ["tariff"],
        "context_terms": [
            "policy",
            "increase",
            "exemption",
            "rate",
            "announced",
            "effective",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
        "active": False,
    },
)


LEGACY_TARIFF_KEYWORD_DEFAULTS = {
    "医疗手套关税（中文）": {
        "match_terms": ["丁腈手套", "医用手套", "一次性手套"],
        "context_terms": ["关税", "加征关税"],
        "exclude_terms": ["拳击", "足球"],
        "lookback_days": 365,
    },
    "贸易救济案件（中文）": {
        "match_terms": ["反倾销税", "反补贴税", "保障措施", "贸易救济"],
        "context_terms": [
            "立案调查",
            "初裁",
            "终裁",
            "行政复审",
            "日落复审",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
    },
    "贸易救济案件（英文）": {
        "match_terms": [
            "anti-dumping duty",
            "countervailing duty",
            "safeguard measure",
        ],
        "context_terms": [
            "investigation",
            "preliminary determination",
            "final determination",
            "review",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
    },
    "通用关税政策（中文）": {
        "match_terms": [
            "加征关税",
            "关税调整",
            "进口关税",
            "对等关税",
            "进口附加费",
        ],
        "context_terms": ["公告", "实施", "税率", "豁免", "暂停"],
        "exclude_terms": [],
        "lookback_days": 30,
    },
    "通用关税政策（英文）": {
        "match_terms": [
            "tariff increase",
            "import tariff",
            "reciprocal tariff",
            "import surcharge",
        ],
        "context_terms": [
            "announced",
            "effective",
            "exemption",
            "suspension",
        ],
        "exclude_terms": [],
        "lookback_days": 30,
    },
}

KEYWORD_CATEGORY_RELEVANCE_SQL = """
    rr.status = 'success'
    AND rr.is_relevant = 1
    AND (
        kc.name IS NULL
        OR kc.name NOT IN ('贸易政策', '关税调整', '行业法规')
        OR (
            rr.prompt_version IN ('intco-relevance-v7', 'intco-relevance-v8')
            AND (
                (kc.name = '贸易政策' AND rr.keyword_categories LIKE '%"贸易政策"%')
                OR (kc.name = '关税调整' AND rr.keyword_categories LIKE '%"关税调整"%')
                OR (kc.name = '行业法规' AND rr.keyword_categories LIKE '%"行业法规"%')
            )
        )
        OR (
            rr.prompt_version NOT IN ('intco-relevance-v7', 'intco-relevance-v8')
            AND (
                (
                    kc.name IN ('贸易政策', '行业法规')
                    AND (
                        rr.category = 'policy_regulation'
                        OR rr.secondary_categories LIKE '%"policy_regulation"%'
                    )
                )
                OR (
                    kc.name = '关税调整'
                    AND (
                        rr.category = 'trade_tariff'
                        OR rr.secondary_categories LIKE '%"trade_tariff"%'
                    )
                )
            )
        )
    )
"""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, target: Path | str) -> None:
        target_text = str(target)
        if target_text.lower().startswith(("mysql://", "mysql+pymysql://")):
            self.backend = "mysql"
            self.path: Path | None = None
            self._mysql_settings = parse_mysql_url(target_text)
        else:
            self.backend = "sqlite"
            self.path = Path(target)
            self._mysql_settings = None

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self._mysql_settings is not None:
            connection: Any = MySQLConnection(self._mysql_settings)
        else:
            assert self.path is not None
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            source_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(rss_sources)").fetchall()
            }
            if "country" not in source_columns:
                connection.execute(
                    "ALTER TABLE rss_sources ADD COLUMN country TEXT NOT NULL DEFAULT ''"
                )
            if "site_domain" not in source_columns:
                connection.execute(
                    """
                    ALTER TABLE rss_sources
                    ADD COLUMN site_domain TEXT NOT NULL DEFAULT ''
                    """
                )
            if "crawler_enabled" not in source_columns:
                connection.execute(
                    """
                    ALTER TABLE rss_sources
                    ADD COLUMN crawler_enabled INTEGER NOT NULL DEFAULT 0
                    """
                )
            if self.backend == "mysql":
                crawler_source_migrations = {
                    "crawler_failure_kind": "VARCHAR(40) NOT NULL DEFAULT ''",
                    "crawler_failure_count": "INT NOT NULL DEFAULT 0",
                    "crawler_cooldown_until": "VARCHAR(40) NULL",
                    "crawler_last_error": "LONGTEXT NOT NULL DEFAULT ('')",
                    "crawler_last_success_at": "VARCHAR(40) NULL",
                }
            else:
                crawler_source_migrations = {
                    "crawler_failure_kind": "TEXT NOT NULL DEFAULT ''",
                    "crawler_failure_count": "INTEGER NOT NULL DEFAULT 0",
                    "crawler_cooldown_until": "TEXT",
                    "crawler_last_error": "TEXT NOT NULL DEFAULT ''",
                    "crawler_last_success_at": "TEXT",
                }
            source_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(rss_sources)"
                ).fetchall()
            }
            for column, definition in crawler_source_migrations.items():
                if column not in source_columns:
                    connection.execute(
                        f"""
                        ALTER TABLE rss_sources
                        ADD COLUMN {column} {definition}
                        """  # noqa: S608
                    )
            keyword_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(keywords)").fetchall()
            }
            if "context_terms" not in keyword_columns:
                connection.execute(
                    "ALTER TABLE keywords ADD COLUMN context_terms TEXT NOT NULL DEFAULT '[]'"
                )
            if "exclude_terms" not in keyword_columns:
                connection.execute(
                    "ALTER TABLE keywords ADD COLUMN exclude_terms TEXT NOT NULL DEFAULT '[]'"
                )
            if "lookback_days" not in keyword_columns:
                connection.execute(
                    "ALTER TABLE keywords ADD COLUMN lookback_days INTEGER NOT NULL DEFAULT 30"
                )
            added_require_local_match = "require_local_match" not in keyword_columns
            if added_require_local_match:
                connection.execute(
                    """
                    ALTER TABLE keywords
                    ADD COLUMN require_local_match INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "category_id" not in keyword_columns:
                connection.execute(
                    """
                    ALTER TABLE keywords
                    ADD COLUMN category_id INTEGER
                    REFERENCES keyword_categories(id) ON DELETE SET NULL
                    """
                )
            article_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(articles)").fetchall()
            }
            if "publisher_normalized" not in article_columns:
                connection.execute(
                    """
                    ALTER TABLE articles
                    ADD COLUMN publisher_normalized TEXT NOT NULL DEFAULT ''
                    """
                )
            content_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(article_contents)"
                ).fetchall()
            }
            content_migrations = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "failure_kind": "TEXT NOT NULL DEFAULT ''",
                "next_retry_at": "TEXT",
                "is_terminal": "INTEGER NOT NULL DEFAULT 0",
                "ignored_at": "TEXT",
            }
            for column, definition in content_migrations.items():
                if column not in content_columns:
                    connection.execute(
                        f"ALTER TABLE article_contents ADD COLUMN {column} {definition}"
                    )
            review_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(article_relevance_reviews)"
                ).fetchall()
            }
            review_migrations = {
                "category": "TEXT NOT NULL DEFAULT 'other'",
                "secondary_categories": "TEXT NOT NULL DEFAULT '[]'",
                "keyword_categories": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, definition in review_migrations.items():
                if column not in review_columns:
                    connection.execute(
                        f"""
                        ALTER TABLE article_relevance_reviews
                        ADD COLUMN {column} {definition}
                        """  # noqa: S608
                    )
            detail_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(collection_run_details)"
                ).fetchall()
            }
            if "skipped_outside_window" not in detail_columns:
                connection.execute(
                    """
                    ALTER TABLE collection_run_details
                    ADD COLUMN skipped_outside_window INTEGER NOT NULL DEFAULT 0
                    """
                )
            ai_item_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(ai_analysis_run_items)"
                ).fetchall()
            }
            for column in (
                "content_status",
                "relevance_status",
                "business_analysis_status",
            ):
                if column not in ai_item_columns:
                    connection.execute(
                        f"""
                        ALTER TABLE ai_analysis_run_items
                        ADD COLUMN {column} TEXT NOT NULL DEFAULT 'pending'
                        """  # noqa: S608
                    )
            report_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(daily_reports)"
                ).fetchall()
            }
            report_migrations = {
                "keyword_category_id": (
                    "INTEGER REFERENCES keyword_categories(id) ON DELETE SET NULL"
                ),
                "keyword_category_name": "TEXT NOT NULL DEFAULT ''",
                "overview": (
                    "LONGTEXT NOT NULL DEFAULT ('')"
                    if self.backend == "mysql"
                    else "TEXT NOT NULL DEFAULT ''"
                ),
                "details": (
                    "JSON NOT NULL DEFAULT (JSON_ARRAY())"
                    if self.backend == "mysql"
                    else "TEXT NOT NULL DEFAULT '[]'"
                ),
            }
            for column, definition in report_migrations.items():
                if column not in report_columns:
                    connection.execute(
                        f"""
                        ALTER TABLE daily_reports
                        ADD COLUMN {column} {definition}
                        """  # noqa: S608
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_daily_reports_keyword_category
                ON daily_reports(
                    report_date DESC, keyword_category_id, status, id DESC
                )
                """
            )
            source_rows = connection.execute(
                "SELECT id, url_template, language, country FROM rss_sources"
            ).fetchall()
            for source_row in source_rows:
                if not source_row["country"]:
                    connection.execute(
                        "UPDATE rss_sources SET country = ? WHERE id = ?",
                        (
                            infer_country(
                                source_row["url_template"], source_row["language"]
                            ),
                            source_row["id"],
                        ),
                    )
            article_rows = connection.execute(
                "SELECT id, publisher, publisher_normalized FROM articles"
            ).fetchall()
            for article_row in article_rows:
                normalized = normalize_publisher(article_row["publisher"])
                if normalized != article_row["publisher_normalized"]:
                    connection.execute(
                        "UPDATE articles SET publisher_normalized = ? WHERE id = ?",
                        (normalized, article_row["id"]),
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO article_sources
                    (article_id, rss_source_id, feed_url, observed_url,
                     canonical_url, guid, language, country, categories,
                     first_seen_at, last_seen_at)
                SELECT a.id, a.rss_source_id, '', a.url,
                       COALESCE(a.canonical_url, a.url), '',
                       COALESCE(s.language, ''), COALESCE(s.country, ''), '[]',
                       a.collected_at, a.collected_at
                FROM articles a
                JOIN rss_sources s ON s.id = a.rss_source_id
                WHERE a.rss_source_id IS NOT NULL
                """
            )
            connection.execute(
                """
                UPDATE collection_runs
                SET status = 'interrupted', finished_at = ?,
                    message = CASE
                        WHEN message = '' THEN '应用重启，运行被中断'
                        ELSE message
                    END
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE article_contents
                SET attempt_count = 0, next_retry_at = NULL,
                    is_terminal = 0, ignored_at = NULL,
                    failure_kind = 'llm_migration_pending',
                    error_message = '正文读取方式已切换为 OpenAI 网页搜索，等待重新读取'
                WHERE status = 'failed'
                  AND (
                    failure_kind LIKE 'http_%'
                    OR failure_kind IN (
                        'network', 'dns', 'resolver', 'extraction',
                        'content_type', 'too_large'
                    )
                  )
                """
            )
            connection.execute(
                """
                DELETE FROM article_contents
                WHERE status = 'failed'
                  AND content_hash = ''
                  AND full_text = ''
                  AND failure_kind = 'llm_migration_pending'
                """
            )
            connection.execute(
                """
                DELETE FROM article_contents
                WHERE status = 'success'
                  AND extractor <> ''
                  AND extractor <> 'openai-web-search'
                """
            )
            connection.execute(
                """
                UPDATE ai_analysis_runs
                SET status = 'interrupted', finished_at = ?,
                    message = CASE
                        WHEN message = '' THEN '应用重启，AI 分析被中断'
                        ELSE message
                    END
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE article_analyses
                SET status = 'failed', error_message = CASE
                    WHEN error_message = '' THEN '应用重启，AI 分析被中断'
                    ELSE error_message
                END
                WHERE status = 'processing'
                """
            )
            connection.execute(
                """
                UPDATE article_contents
                SET status = 'failed', error_message = CASE
                    WHEN error_message = '' THEN '应用重启，大模型全文读取被中断'
                    ELSE error_message
                END
                WHERE status = 'processing'
                """
            )
            connection.execute(
                """
                UPDATE article_relevance_reviews
                SET status = 'failed', error_message = CASE
                    WHEN error_message = '' THEN '应用重启，相关性审核被中断'
                    ELSE error_message
                END
                WHERE status = 'processing'
                """
            )
            connection.execute(
                """
                UPDATE business_articles
                SET analysis_status = 'failed', error_message = CASE
                    WHEN error_message = '' THEN '应用重启，业务分析被中断'
                    ELSE error_message
                END
                WHERE analysis_status = 'processing'
                """
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed',
                    content_status = CASE
                        WHEN content_status = 'processing' THEN 'failed'
                        ELSE content_status
                    END,
                    relevance_status = CASE
                        WHEN relevance_status = 'processing' THEN 'failed'
                        ELSE relevance_status
                    END,
                    business_analysis_status = CASE
                        WHEN business_analysis_status = 'processing' THEN 'failed'
                        ELSE business_analysis_status
                    END,
                    error_message = CASE
                    WHEN error_message = '' THEN '应用重启，AI 分析被中断'
                    ELSE error_message
                END
                WHERE status IN ('pending', 'processing')
                """
            )
            connection.execute(
                """
                UPDATE daily_reports
                SET status = 'interrupted', updated_at = ?,
                    error_message = CASE
                        WHEN error_message = '' THEN '应用重启，日报生成被中断'
                        ELSE error_message
                    END
                WHERE status = 'running'
                """,
                (now,),
            )
            for sort_order, category_name in enumerate(DEFAULT_KEYWORD_CATEGORIES):
                connection.execute(
                    """
                    INSERT INTO keyword_categories
                        (name, sort_order, active, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        sort_order = excluded.sort_order,
                        updated_at = excluded.updated_at
                    """,
                    (category_name, sort_order, now, now),
                )
            for source in DEFAULT_SOURCES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO rss_sources
                        (name, url_template, mode, language, country, site_domain,
                         active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source["name"],
                        source["url_template"],
                        source["mode"],
                        source["language"],
                        source["country"],
                        source.get("site_domain", ""),
                        int(source["active"]),
                        now,
                        now,
                    ),
                )
            for keyword in DEFAULT_KEYWORDS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO keywords
                        (category_id, name, query, match_terms, context_terms,
                         exclude_terms, lookback_days, require_local_match, active,
                         created_at, updated_at)
                    VALUES (
                        (SELECT id FROM keyword_categories WHERE name = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        keyword.get("category_name"),
                        keyword["name"],
                        build_keyword_query(
                            keyword["match_terms"],
                            context_terms=keyword["context_terms"],
                            exclude_terms=keyword["exclude_terms"],
                            lookback_days=keyword["lookback_days"],
                        ),
                        json.dumps(keyword["match_terms"], ensure_ascii=False),
                        json.dumps(keyword["context_terms"], ensure_ascii=False),
                        json.dumps(keyword["exclude_terms"], ensure_ascii=False),
                        keyword["lookback_days"],
                        int(keyword.get("require_local_match", False)),
                        int(keyword["active"]),
                        now,
                        now,
                    ),
                )
            if added_require_local_match:
                for keyword in DEFAULT_KEYWORDS:
                    if not keyword.get("require_local_match", False):
                        continue
                    connection.execute(
                        """
                        UPDATE keywords
                        SET require_local_match = 1, updated_at = ?
                        WHERE name = ? AND archived = 0
                        """,
                        (now, keyword["name"]),
                    )
                    connection.execute(
                        """
                        DELETE FROM article_keywords
                        WHERE keyword_id IN (
                            SELECT id FROM keywords
                            WHERE name = ? AND archived = 0
                        )
                        """,
                        (keyword["name"],),
                    )
                    connection.execute(
                        """
                        DELETE FROM collection_cursors
                        WHERE keyword_id IN (
                            SELECT id FROM keywords
                            WHERE name = ? AND archived = 0
                        )
                        """,
                        (keyword["name"],),
                    )
            default_keywords_by_name = {
                keyword["name"]: keyword for keyword in DEFAULT_KEYWORDS
            }
            for name, legacy_strategy in LEGACY_TARIFF_KEYWORD_DEFAULTS.items():
                keyword_row = connection.execute(
                    """
                    SELECT id, match_terms, context_terms, exclude_terms,
                           lookback_days
                    FROM keywords
                    WHERE name = ? AND archived = 0
                    """,
                    (name,),
                ).fetchone()
                if keyword_row is None:
                    continue
                try:
                    current_strategy = {
                        "match_terms": json.loads(keyword_row["match_terms"]),
                        "context_terms": json.loads(keyword_row["context_terms"]),
                        "exclude_terms": json.loads(keyword_row["exclude_terms"]),
                        "lookback_days": int(keyword_row["lookback_days"]),
                    }
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if current_strategy != legacy_strategy:
                    continue
                current_default = default_keywords_by_name[name]
                generated_query = build_keyword_query(
                    current_default["match_terms"],
                    context_terms=current_default["context_terms"],
                    exclude_terms=current_default["exclude_terms"],
                    lookback_days=current_default["lookback_days"],
                )
                connection.execute(
                    """
                    UPDATE keywords
                    SET query = ?, match_terms = ?, context_terms = ?,
                        exclude_terms = ?, lookback_days = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        generated_query,
                        json.dumps(
                            current_default["match_terms"], ensure_ascii=False
                        ),
                        json.dumps(
                            current_default["context_terms"], ensure_ascii=False
                        ),
                        json.dumps(
                            current_default["exclude_terms"], ensure_ascii=False
                        ),
                        current_default["lookback_days"],
                        now,
                        keyword_row["id"],
                    ),
                )
            keyword_rows = connection.execute(
                """
                SELECT id, query, match_terms, context_terms,
                       exclude_terms, lookback_days
                FROM keywords
                """
            ).fetchall()
            for keyword_row in keyword_rows:
                try:
                    generated_query = build_keyword_query(
                        json.loads(keyword_row["match_terms"]),
                        context_terms=json.loads(keyword_row["context_terms"]),
                        exclude_terms=json.loads(keyword_row["exclude_terms"]),
                        lookback_days=int(keyword_row["lookback_days"]),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if generated_query != keyword_row["query"]:
                    connection.execute(
                        "UPDATE keywords SET query = ?, updated_at = ? WHERE id = ?",
                        (generated_query, now, keyword_row["id"]),
                    )
            defaults = {
                "schedule_time": "08:00",
                "timezone": "Asia/Shanghai",
                "incremental_collection": "true",
                "search_local_keyword_filter": "true",
                "crawler_enabled": "false",
                "crawler_respect_robots": "true",
                "crawler_min_interval_seconds": "3",
                "crawler_cooldown_minutes": "60",
                "ai_business_profile": DEFAULT_BUSINESS_PROFILE,
                "ai_relevance_prompt": DEFAULT_RELEVANCE_PROMPT,
                "ai_report_prompt": DEFAULT_REPORT_PROMPT,
                "ai_relevance_threshold": "70",
                "ai_batch_size": "20",
                "ai_parallelism": "4",
                "ai_content_max_chars": "30000",
                "ai_auto_analyze": "false",
                "ai_auto_report": "false",
                "feishu_auto_push": "false",
            }
            for key, value in defaults.items():
                connection.execute(
                    """
                    INSERT INTO app_settings (`key`, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(`key`) DO NOTHING
                    """,
                    (key, value, now),
                )
            prompt_default_migrations = {
                "ai_business_profile": (
                    [
                        LEGACY_DEFAULT_BUSINESS_PROFILE,
                        LEGACY_DEFAULT_BUSINESS_PROFILE_V4,
                        LEGACY_DEFAULT_BUSINESS_PROFILE_V5,
                    ],
                    DEFAULT_BUSINESS_PROFILE,
                ),
                "ai_relevance_prompt": (
                    [
                        LEGACY_DEFAULT_RELEVANCE_PROMPT_V4,
                        LEGACY_DEFAULT_RELEVANCE_PROMPT_V5,
                        LEGACY_DEFAULT_RELEVANCE_PROMPT_V6,
                        LEGACY_DEFAULT_RELEVANCE_PROMPT_V7,
                    ],
                    DEFAULT_RELEVANCE_PROMPT,
                ),
                "ai_report_prompt": (
                    [
                        LEGACY_DEFAULT_REPORT_PROMPT,
                        LEGACY_DEFAULT_REPORT_PROMPT_V4,
                        LEGACY_DEFAULT_REPORT_PROMPT_V5,
                        LEGACY_DEFAULT_REPORT_PROMPT_V6,
                        LEGACY_DEFAULT_REPORT_PROMPT_V7,
                        LEGACY_DEFAULT_REPORT_PROMPT_V8,
                        LEGACY_DEFAULT_REPORT_PROMPT_V9,
                    ],
                    DEFAULT_REPORT_PROMPT,
                ),
            }
            for key, (legacy_values, current_value) in (
                prompt_default_migrations.items()
            ):
                for legacy_value in legacy_values:
                    connection.execute(
                        """
                        UPDATE app_settings
                        SET value = ?, updated_at = ?
                        WHERE `key` = ? AND value = ?
                        """,
                        (current_value, now, key, legacy_value),
                    )

    @staticmethod
    def rows(rows: list[Any]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_sources(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE archived = 0"
        if active_only:
            where += " AND active = 1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM rss_sources {where} ORDER BY id"  # noqa: S608
            ).fetchall()
        result = self.rows(rows)
        now = datetime.now(UTC)
        for item in result:
            if item.pop("crawler_enabled", 0):
                item["mode"] = "crawler"
            cooldown_until = item.get("crawler_cooldown_until")
            try:
                parsed_cooldown = (
                    datetime.fromisoformat(
                        str(cooldown_until).replace("Z", "+00:00")
                    )
                    if cooldown_until
                    else None
                )
            except ValueError:
                parsed_cooldown = None
            if parsed_cooldown is not None and parsed_cooldown.tzinfo is None:
                parsed_cooldown = parsed_cooldown.replace(tzinfo=UTC)
            item["crawler_in_cooldown"] = bool(
                parsed_cooldown and parsed_cooldown > now
            )
        return result

    def get_keyword_categories(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, name, sort_order
                FROM keyword_categories
                WHERE active = 1
                ORDER BY sort_order, id
                """
            ).fetchall()
        return self.rows(rows)

    def get_keyword_category(self, category_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, sort_order
                FROM keyword_categories
                WHERE id = ? AND active = 1
                """,
                (category_id,),
            ).fetchone()
        return dict(row) if row else None

    def keyword_hit_stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    k.id AS keyword_id,
                    k.name AS keyword_name,
                    k.active,
                    k.category_id,
                    kc.name AS category_name,
                    COUNT(DISTINCT ak.article_id) AS hit_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success' THEN ak.article_id
                    END) AS reviewed_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success'
                         AND rr.is_relevant = 1
                        THEN ak.article_id
                    END) AS business_relevant_count,
                    COUNT(DISTINCT CASE
                        WHEN {KEYWORD_CATEGORY_RELEVANCE_SQL}
                        THEN ak.article_id
                    END) AS relevant_count
                FROM keywords k
                LEFT JOIN keyword_categories kc ON kc.id = k.category_id
                LEFT JOIN article_keywords ak ON ak.keyword_id = k.id
                LEFT JOIN article_relevance_reviews rr
                  ON rr.article_id = ak.article_id
                WHERE k.archived = 0
                GROUP BY k.id, k.name, k.active, k.category_id, kc.name
                ORDER BY COALESCE(kc.sort_order, 999999), k.id
                """  # noqa: S608
            ).fetchall()
            category_rows = connection.execute(
                f"""
                SELECT
                    kc.id AS category_id,
                    kc.name AS category_name,
                    kc.sort_order,
                    COUNT(DISTINCT k.id) AS keyword_count,
                    COUNT(DISTINCT ak.article_id) AS hit_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success' THEN ak.article_id
                    END) AS reviewed_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success'
                         AND rr.is_relevant = 1
                        THEN ak.article_id
                    END) AS business_relevant_count,
                    COUNT(DISTINCT CASE
                        WHEN {KEYWORD_CATEGORY_RELEVANCE_SQL}
                        THEN ak.article_id
                    END) AS relevant_count
                FROM keyword_categories kc
                LEFT JOIN keywords k
                  ON k.category_id = kc.id
                 AND k.archived = 0
                 AND k.active = 1
                LEFT JOIN article_keywords ak ON ak.keyword_id = k.id
                LEFT JOIN article_relevance_reviews rr
                  ON rr.article_id = ak.article_id
                WHERE kc.active = 1
                GROUP BY kc.id, kc.name, kc.sort_order
                UNION ALL
                SELECT
                    NULL AS category_id,
                    NULL AS category_name,
                    999999 AS sort_order,
                    COUNT(DISTINCT k.id) AS keyword_count,
                    COUNT(DISTINCT ak.article_id) AS hit_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success' THEN ak.article_id
                    END) AS reviewed_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success'
                         AND rr.is_relevant = 1
                        THEN ak.article_id
                    END) AS business_relevant_count,
                    COUNT(DISTINCT CASE
                        WHEN {KEYWORD_CATEGORY_RELEVANCE_SQL}
                        THEN ak.article_id
                    END) AS relevant_count
                FROM keywords k
                LEFT JOIN keyword_categories kc ON kc.id = k.category_id
                LEFT JOIN article_keywords ak ON ak.keyword_id = k.id
                LEFT JOIN article_relevance_reviews rr
                  ON rr.article_id = ak.article_id
                WHERE k.archived = 0
                  AND k.active = 1
                  AND k.category_id IS NULL
                ORDER BY sort_order, category_id
                """  # noqa: S608
            ).fetchall()
            overall_row = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT k.id) AS keyword_count,
                    COUNT(DISTINCT ak.article_id) AS hit_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success' THEN ak.article_id
                    END) AS reviewed_count,
                    COUNT(DISTINCT CASE
                        WHEN rr.status = 'success'
                         AND rr.is_relevant = 1
                        THEN ak.article_id
                    END) AS business_relevant_count,
                    COUNT(DISTINCT CASE
                        WHEN {KEYWORD_CATEGORY_RELEVANCE_SQL}
                        THEN ak.article_id
                    END) AS relevant_count
                FROM keywords k
                LEFT JOIN keyword_categories kc ON kc.id = k.category_id
                LEFT JOIN article_keywords ak ON ak.keyword_id = k.id
                LEFT JOIN article_relevance_reviews rr
                  ON rr.article_id = ak.article_id
                WHERE k.archived = 0 AND k.active = 1
                """  # noqa: S608
            ).fetchone()

        def add_rates(item: dict[str, Any]) -> dict[str, Any]:
            reviewed = int(item["reviewed_count"] or 0)
            relevant = int(item["relevant_count"] or 0)
            item["business_relevant_count"] = int(
                item["business_relevant_count"] or 0
            )
            hit_count = int(item["hit_count"] or 0)
            item["pending_review_count"] = max(0, hit_count - reviewed)
            item["hit_rate"] = relevant / reviewed if reviewed else None
            return item

        keywords: list[dict[str, Any]] = []
        for row in rows:
            keywords.append(add_rates(dict(row)))

        categories: list[dict[str, Any]] = []
        for row in category_rows:
            item = dict(row)
            item.pop("sort_order", None)
            item["category_name"] = item["category_name"] or "未分类"
            categories.append(add_rates(item))

        overall = add_rates(dict(overall_row))
        return {
            "overall": overall,
            "categories": categories,
            "keywords": keywords,
        }

    def get_keywords(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE k.archived = 0"
        if active_only:
            where += " AND k.active = 1"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT k.*, kc.name AS category_name
                FROM keywords k
                LEFT JOIN keyword_categories kc ON kc.id = k.category_id
                {where}
                ORDER BY COALESCE(kc.sort_order, 999999), k.id
                """  # noqa: S608
            ).fetchall()
        result = self.rows(rows)
        for item in result:
            item["match_terms"] = json.loads(item["match_terms"])
            item["context_terms"] = json.loads(item["context_terms"])
            item["exclude_terms"] = json.loads(item["exclude_terms"])
        return result

    def create_source(self, data: dict[str, Any]) -> int:
        now = utc_now_iso()
        crawler_enabled = data["mode"] == "crawler"
        storage_mode = "direct" if crawler_enabled else data["mode"]
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rss_sources
                    (name, url_template, mode, language, country, site_domain,
                     crawler_enabled, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    data["url_template"],
                    storage_mode,
                    data.get("language", ""),
                    data.get("country")
                    or infer_country(data["url_template"], data.get("language", "")),
                    data.get("site_domain", ""),
                    int(crawler_enabled),
                    int(data.get("active", True)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_source(self, source_id: int, data: dict[str, Any]) -> bool:
        now = utc_now_iso()
        crawler_enabled = data["mode"] == "crawler"
        storage_mode = "direct" if crawler_enabled else data["mode"]
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE rss_sources
                SET name = ?, url_template = ?, mode = ?, language = ?, country = ?,
                    site_domain = ?, crawler_enabled = ?, active = ?, updated_at = ?
                WHERE id = ? AND archived = 0
                """,
                (
                    data["name"],
                    data["url_template"],
                    storage_mode,
                    data.get("language", ""),
                    data.get("country")
                    or infer_country(data["url_template"], data.get("language", "")),
                    data.get("site_domain", ""),
                    int(crawler_enabled),
                    int(data.get("active", True)),
                    now,
                    source_id,
                ),
            )
            return cursor.rowcount > 0

    def archive_source(self, source_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE rss_sources SET active = 0, archived = 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), source_id),
            )
            return cursor.rowcount > 0

    def create_keyword(self, data: dict[str, Any]) -> int:
        now = utc_now_iso()
        context_terms = data.get("context_terms", [])
        exclude_terms = data.get("exclude_terms", [])
        lookback_days = int(data.get("lookback_days", 30))
        query = build_keyword_query(
            data["match_terms"],
            context_terms=context_terms,
            exclude_terms=exclude_terms,
            lookback_days=lookback_days,
        )
        with self.connect() as connection:
            category_id = data.get("category_id")
            if category_id is not None and connection.execute(
                "SELECT 1 FROM keyword_categories WHERE id = ? AND active = 1",
                (category_id,),
            ).fetchone() is None:
                raise ValueError("关键词分类不存在")
            cursor = connection.execute(
                """
                INSERT INTO keywords
                    (category_id, name, query, match_terms, context_terms,
                     exclude_terms, lookback_days, require_local_match, active,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    category_id,
                    data["name"],
                    query,
                    json.dumps(data["match_terms"], ensure_ascii=False),
                    json.dumps(context_terms, ensure_ascii=False),
                    json.dumps(exclude_terms, ensure_ascii=False),
                    lookback_days,
                    int(data.get("require_local_match", False)),
                    int(data.get("active", True)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_keyword(self, keyword_id: int, data: dict[str, Any]) -> bool:
        now = utc_now_iso()
        context_terms = data.get("context_terms", [])
        exclude_terms = data.get("exclude_terms", [])
        lookback_days = int(data.get("lookback_days", 30))
        query = build_keyword_query(
            data["match_terms"],
            context_terms=context_terms,
            exclude_terms=exclude_terms,
            lookback_days=lookback_days,
        )
        with self.connect() as connection:
            category_id = data.get("category_id")
            if category_id is not None and connection.execute(
                "SELECT 1 FROM keyword_categories WHERE id = ? AND active = 1",
                (category_id,),
            ).fetchone() is None:
                raise ValueError("关键词分类不存在")
            current = connection.execute(
                """
                SELECT match_terms, context_terms, exclude_terms, lookback_days,
                       require_local_match
                FROM keywords
                WHERE id = ? AND archived = 0
                """,
                (keyword_id,),
            ).fetchone()
            if current is None:
                return False
            strategy_changed = any(
                (
                    current["match_terms"]
                    != json.dumps(data["match_terms"], ensure_ascii=False),
                    current["context_terms"]
                    != json.dumps(context_terms, ensure_ascii=False),
                    current["exclude_terms"]
                    != json.dumps(exclude_terms, ensure_ascii=False),
                    int(current["lookback_days"]) != lookback_days,
                    bool(current["require_local_match"])
                    != bool(data.get("require_local_match", False)),
                )
            )
            cursor = connection.execute(
                """
                UPDATE keywords
                SET category_id = ?, name = ?, query = ?, match_terms = ?,
                    context_terms = ?, exclude_terms = ?, lookback_days = ?,
                    require_local_match = ?, active = ?, updated_at = ?
                WHERE id = ? AND archived = 0
                """,
                (
                    category_id,
                    data["name"],
                    query,
                    json.dumps(data["match_terms"], ensure_ascii=False),
                    json.dumps(context_terms, ensure_ascii=False),
                    json.dumps(exclude_terms, ensure_ascii=False),
                    lookback_days,
                    int(data.get("require_local_match", False)),
                    int(data.get("active", True)),
                    now,
                    keyword_id,
                ),
            )
            if strategy_changed:
                connection.execute(
                    "DELETE FROM article_keywords WHERE keyword_id = ?",
                    (keyword_id,),
                )
                connection.execute(
                    "DELETE FROM collection_cursors WHERE keyword_id = ?",
                    (keyword_id,),
                )
            return cursor.rowcount > 0

    def archive_keyword(self, keyword_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE keywords SET active = 0, archived = 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), keyword_id),
            )
            return cursor.rowcount > 0

    def create_backup(self, backup_dir: Path) -> Path:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = ".sql" if self.backend == "mysql" else ".db"
        backup_path = backup_dir / f"rss_collector-before-cleanup-{timestamp}{suffix}"
        if self._mysql_settings is not None:
            with self.connect() as connection:
                dump_mysql_database(connection, backup_path)
        else:
            assert self.path is not None
            with sqlite3.connect(self.path) as source:
                with sqlite3.connect(backup_path) as destination:
                    source.backup(destination)
        return backup_path

    def get_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT `key`, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (`key`, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(`key`) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def create_run(self, trigger_type: str, started_at: str, window_start: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_runs
                    (trigger_type, status, started_at, window_start, window_end)
                VALUES (?, 'running', ?, ?, ?)
                """,
                (trigger_type, started_at, window_start, started_at),
            )
            return int(cursor.lastrowid)

    def fail_run(self, run_id: int, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE collection_runs
                SET status = 'failed', finished_at = ?, message = ?
                WHERE id = ?
                """,
                (utc_now_iso(), message[:2000], run_id),
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return self.rows(rows)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            run = connection.execute(
                "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            details = connection.execute(
                """
                SELECT d.*, s.name AS source_name, k.name AS keyword_name
                FROM collection_run_details d
                JOIN rss_sources s ON s.id = d.rss_source_id
                JOIN keywords k ON k.id = d.keyword_id
                WHERE d.run_id = ?
                ORDER BY d.id
                """,
                (run_id,),
            ).fetchall()
        result = dict(run)
        result["details"] = self.rows(details)
        return result

    def latest_run(self) -> dict[str, Any] | None:
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    def list_articles(
        self,
        *,
        query: str = "",
        source_id: int | None = None,
        keyword_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters: list[str] = []
        parameters: list[Any] = []
        if query:
            filters.append(
                "(a.title LIKE ? OR a.summary LIKE ? OR a.publisher LIKE ? "
                "OR a.publisher_normalized LIKE ?)"
            )
            term = f"%{query}%"
            parameters.extend((term, term, term, term))
        if source_id is not None:
            filters.append(
                "EXISTS (SELECT 1 FROM article_sources axs "
                "WHERE axs.article_id = a.id AND axs.rss_source_id = ?)"
            )
            parameters.append(source_id)
        if keyword_id is not None:
            filters.append(
                "EXISTS (SELECT 1 FROM article_keywords ak2 WHERE ak2.article_id = a.id AND ak2.keyword_id = ?)"
            )
            parameters.append(keyword_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM articles a {where}",  # noqa: S608
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT a.id, a.title, a.url, a.canonical_url, a.fingerprint,
                       a.publisher, a.publisher_normalized, a.summary,
                       a.published_at, a.collected_at, a.rss_source_id,
                       s.name AS feed_name
                FROM articles a
                LEFT JOIN rss_sources s ON s.id = a.rss_source_id
                {where}
                ORDER BY a.published_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                [*parameters, limit, offset],
            ).fetchall()
            items = self.rows(rows)
            article_ids = [item["id"] for item in items]
            if not article_ids:
                return {"total": int(total), "items": []}
            placeholders = ",".join("?" for _ in article_ids)
            source_rows = connection.execute(
                f"""
                SELECT axs.id, axs.article_id, axs.rss_source_id,
                       axs.feed_url, axs.observed_url, axs.canonical_url,
                       axs.guid, axs.language, axs.country, axs.categories,
                       axs.first_seen_at, axs.last_seen_at,
                       s.name AS source_name
                FROM article_sources axs
                JOIN rss_sources s ON s.id = axs.rss_source_id
                WHERE axs.article_id IN ({placeholders})
                ORDER BY axs.first_seen_at, axs.id
                """,  # noqa: S608
                article_ids,
            ).fetchall()
            keyword_rows = connection.execute(
                f"""
                SELECT ak.article_id, ak.keyword_id, ak.matched_terms,
                       k.name AS keyword_name
                FROM article_keywords ak
                JOIN keywords k ON k.id = ak.keyword_id
                WHERE ak.article_id IN ({placeholders})
                ORDER BY k.name
                """,  # noqa: S608
                article_ids,
            ).fetchall()

        sources_by_article: dict[int, list[dict[str, Any]]] = {
            article_id: [] for article_id in article_ids
        }
        for row in source_rows:
            source = dict(row)
            try:
                source["categories"] = json.loads(source["categories"])
            except (TypeError, json.JSONDecodeError):
                source["categories"] = []
            sources_by_article[source["article_id"]].append(source)

        keywords_by_article: dict[int, list[dict[str, Any]]] = {
            article_id: [] for article_id in article_ids
        }
        for row in keyword_rows:
            keyword = dict(row)
            try:
                keyword["matched_terms"] = json.loads(keyword["matched_terms"])
            except (TypeError, json.JSONDecodeError):
                keyword["matched_terms"] = []
            keywords_by_article[keyword["article_id"]].append(keyword)

        for item in items:
            sources = sources_by_article[item["id"]]
            keywords = keywords_by_article[item["id"]]
            item["sources"] = sources
            item["source_names"] = list(
                dict.fromkeys(source["source_name"] for source in sources)
            )
            item["languages"] = list(
                dict.fromkeys(source["language"] for source in sources if source["language"])
            )
            item["countries"] = list(
                dict.fromkeys(source["country"] for source in sources if source["country"])
            )
            item["categories"] = list(
                dict.fromkeys(
                    category
                    for source in sources
                    for category in source["categories"]
                )
            )
            item["keywords"] = keywords
            item["keyword_names"] = ",".join(
                keyword["keyword_name"] for keyword in keywords
            )
        return {"total": int(total), "items": items}

    def article_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
