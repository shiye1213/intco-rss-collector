from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

try:
    import pymysql
    from pymysql.constants import CLIENT
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only when runtime deps are missing
    pymysql = None
    CLIENT = None
    DictCursor = None


class DatabaseBackendError(RuntimeError):
    """Database driver or server operation failed."""


class DatabaseIntegrityError(DatabaseBackendError):
    """A database uniqueness or foreign-key constraint was violated."""


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


def parse_mysql_url(url: str) -> MySQLSettings:
    parsed = urlsplit(url)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("DATABASE_URL 必须使用 mysql:// 或 mysql+pymysql://")
    database = unquote(parsed.path.lstrip("/"))
    if not parsed.hostname or not parsed.username or not database:
        raise ValueError("DATABASE_URL 必须包含主机、用户名和数据库名")
    if not re.fullmatch(r"[A-Za-z0-9_$-]+", database):
        raise ValueError("MySQL 数据库名只能包含字母、数字、下划线、$ 或连字符")
    query = parse_qs(parsed.query)
    charset = query.get("charset", ["utf8mb4"])[0]
    if not re.fullmatch(r"[A-Za-z0-9_]+", charset):
        raise ValueError("MySQL charset 参数无效")
    return MySQLSettings(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
        database=database,
        charset=charset,
    )


MYSQL_TABLES = (
    "rss_sources",
    "keyword_categories",
    "keywords",
    "articles",
    "article_sources",
    "article_keywords",
    "collection_cursors",
    "collection_runs",
    "collection_run_details",
    "ai_analysis_runs",
    "article_analyses",
    "article_contents",
    "article_relevance_reviews",
    "business_articles",
    "ai_analysis_run_items",
    "daily_reports",
    "daily_report_articles",
    "app_settings",
)


MYSQL_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS rss_sources (
    id BIGINT NOT NULL AUTO_INCREMENT,
    name VARCHAR(191) NOT NULL,
    url_template TEXT NOT NULL,
    mode VARCHAR(16) NOT NULL,
    language VARCHAR(30) NOT NULL DEFAULT '',
    country VARCHAR(10) NOT NULL DEFAULT '',
    site_domain TEXT NOT NULL DEFAULT (''),
    active TINYINT NOT NULL DEFAULT 1,
    archived TINYINT NOT NULL DEFAULT 0,
    created_at VARCHAR(40) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_rss_sources_name (name),
    CONSTRAINT chk_rss_sources_mode CHECK (mode IN ('search', 'direct'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS keyword_categories (
    id BIGINT NOT NULL AUTO_INCREMENT,
    name VARCHAR(191) NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    active TINYINT NOT NULL DEFAULT 1,
    created_at VARCHAR(40) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_keyword_categories_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS keywords (
    id BIGINT NOT NULL AUTO_INCREMENT,
    category_id BIGINT NULL,
    name VARCHAR(191) NOT NULL,
    query LONGTEXT NOT NULL,
    match_terms JSON NOT NULL,
    context_terms JSON NOT NULL DEFAULT (JSON_ARRAY()),
    exclude_terms JSON NOT NULL DEFAULT (JSON_ARRAY()),
    lookback_days INT NOT NULL DEFAULT 30,
    require_local_match TINYINT NOT NULL DEFAULT 0,
    active TINYINT NOT NULL DEFAULT 1,
    archived TINYINT NOT NULL DEFAULT 0,
    created_at VARCHAR(40) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_keywords_name (name),
    KEY idx_keywords_category_id (category_id),
    CONSTRAINT fk_keywords_category FOREIGN KEY (category_id)
        REFERENCES keyword_categories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS articles (
    id BIGINT NOT NULL AUTO_INCREMENT,
    title LONGTEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NULL,
    canonical_url_hash BINARY(32) GENERATED ALWAYS AS (
        CASE
            WHEN canonical_url IS NULL OR canonical_url = '' THEN NULL
            ELSE UNHEX(SHA2(canonical_url, 256))
        END
    ) STORED,
    fingerprint CHAR(64) NOT NULL,
    publisher TEXT NOT NULL DEFAULT (''),
    publisher_normalized VARCHAR(500) NOT NULL DEFAULT '',
    summary LONGTEXT NOT NULL DEFAULT (''),
    published_at VARCHAR(40) NOT NULL,
    collected_at VARCHAR(40) NOT NULL,
    rss_source_id BIGINT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_articles_fingerprint (fingerprint),
    UNIQUE KEY uq_articles_canonical_hash (canonical_url_hash),
    KEY idx_articles_published_at (published_at DESC),
    KEY idx_articles_collected_at (collected_at DESC),
    KEY idx_articles_rss_source_id (rss_source_id),
    CONSTRAINT fk_articles_source FOREIGN KEY (rss_source_id)
        REFERENCES rss_sources(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS article_sources (
    id BIGINT NOT NULL AUTO_INCREMENT,
    article_id BIGINT NOT NULL,
    rss_source_id BIGINT NOT NULL,
    feed_url TEXT NOT NULL DEFAULT (''),
    observed_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT (''),
    canonical_url_hash BINARY(32) GENERATED ALWAYS AS (
        UNHEX(SHA2(canonical_url, 256))
    ) STORED,
    guid TEXT NOT NULL DEFAULT (''),
    language VARCHAR(30) NOT NULL DEFAULT '',
    country VARCHAR(10) NOT NULL DEFAULT '',
    categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    first_seen_at VARCHAR(40) NOT NULL,
    last_seen_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_article_sources_identity (
        article_id, rss_source_id, canonical_url_hash
    ),
    KEY idx_article_sources_article_id (article_id),
    KEY idx_article_sources_source_id (rss_source_id),
    CONSTRAINT fk_article_sources_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT fk_article_sources_source FOREIGN KEY (rss_source_id)
        REFERENCES rss_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS article_keywords (
    article_id BIGINT NOT NULL,
    keyword_id BIGINT NOT NULL,
    matched_terms JSON NOT NULL,
    PRIMARY KEY (article_id, keyword_id),
    CONSTRAINT fk_article_keywords_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT fk_article_keywords_keyword FOREIGN KEY (keyword_id)
        REFERENCES keywords(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collection_cursors (
    rss_source_id BIGINT NOT NULL,
    keyword_id BIGINT NOT NULL,
    last_collected_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (rss_source_id, keyword_id),
    CONSTRAINT fk_collection_cursors_source FOREIGN KEY (rss_source_id)
        REFERENCES rss_sources(id) ON DELETE CASCADE,
    CONSTRAINT fk_collection_cursors_keyword FOREIGN KEY (keyword_id)
        REFERENCES keywords(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collection_runs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    trigger_type VARCHAR(16) NOT NULL,
    status VARCHAR(20) NOT NULL,
    started_at VARCHAR(40) NOT NULL,
    finished_at VARCHAR(40) NULL,
    window_start VARCHAR(40) NOT NULL,
    window_end VARCHAR(40) NOT NULL,
    tasks_total INT NOT NULL DEFAULT 0,
    tasks_succeeded INT NOT NULL DEFAULT 0,
    tasks_failed INT NOT NULL DEFAULT 0,
    items_seen INT NOT NULL DEFAULT 0,
    items_matched INT NOT NULL DEFAULT 0,
    items_inserted INT NOT NULL DEFAULT 0,
    duplicates INT NOT NULL DEFAULT 0,
    message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (id),
    KEY idx_collection_runs_started_at (started_at DESC),
    CONSTRAINT chk_collection_runs_trigger CHECK (
        trigger_type IN ('manual', 'scheduled')
    ),
    CONSTRAINT chk_collection_runs_status CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'interrupted')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collection_run_details (
    id BIGINT NOT NULL AUTO_INCREMENT,
    run_id BIGINT NOT NULL,
    rss_source_id BIGINT NOT NULL,
    keyword_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    window_start VARCHAR(40) NOT NULL,
    window_end VARCHAR(40) NOT NULL,
    feed_url TEXT NOT NULL DEFAULT (''),
    items_seen INT NOT NULL DEFAULT 0,
    items_matched INT NOT NULL DEFAULT 0,
    items_inserted INT NOT NULL DEFAULT 0,
    duplicates INT NOT NULL DEFAULT 0,
    skipped_outside_window INT NOT NULL DEFAULT 0,
    skipped_without_date INT NOT NULL DEFAULT 0,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (id),
    KEY idx_collection_run_details_run_id (run_id),
    KEY idx_collection_run_details_source_id (rss_source_id),
    KEY idx_collection_run_details_keyword_id (keyword_id),
    CONSTRAINT fk_collection_details_run FOREIGN KEY (run_id)
        REFERENCES collection_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_collection_details_source FOREIGN KEY (rss_source_id)
        REFERENCES rss_sources(id) ON DELETE CASCADE,
    CONSTRAINT fk_collection_details_keyword FOREIGN KEY (keyword_id)
        REFERENCES keywords(id) ON DELETE CASCADE,
    CONSTRAINT chk_collection_details_status CHECK (status IN ('success', 'failed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_analysis_runs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    trigger_type VARCHAR(16) NOT NULL,
    status VARCHAR(20) NOT NULL,
    model VARCHAR(255) NOT NULL,
    prompt_version VARCHAR(100) NOT NULL,
    started_at VARCHAR(40) NOT NULL,
    finished_at VARCHAR(40) NULL,
    articles_total INT NOT NULL DEFAULT 0,
    articles_succeeded INT NOT NULL DEFAULT 0,
    articles_failed INT NOT NULL DEFAULT 0,
    relevant_count INT NOT NULL DEFAULT 0,
    irrelevant_count INT NOT NULL DEFAULT 0,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (id),
    KEY idx_ai_analysis_runs_started_at (started_at DESC),
    CONSTRAINT chk_ai_runs_trigger CHECK (trigger_type IN ('manual', 'collection')),
    CONSTRAINT chk_ai_runs_status CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'interrupted')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS article_analyses (
    article_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_relevant TINYINT NOT NULL DEFAULT 0,
    relevance_score INT NOT NULL DEFAULT 0,
    relevance_reason LONGTEXT NOT NULL DEFAULT (''),
    category VARCHAR(100) NOT NULL DEFAULT 'other',
    secondary_categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    summary LONGTEXT NOT NULL DEFAULT (''),
    impact_direction VARCHAR(20) NOT NULL DEFAULT 'neutral',
    impact_score INT NOT NULL DEFAULT 1,
    impact_analysis LONGTEXT NOT NULL DEFAULT (''),
    risk_level VARCHAR(20) NOT NULL DEFAULT 'low',
    risk_score INT NOT NULL DEFAULT 0,
    risk_factors JSON NOT NULL DEFAULT (JSON_ARRAY()),
    opportunities JSON NOT NULL DEFAULT (JSON_ARRAY()),
    recommended_actions JSON NOT NULL DEFAULT (JSON_ARRAY()),
    evidence JSON NOT NULL DEFAULT (JSON_ARRAY()),
    confidence INT NOT NULL DEFAULT 0,
    model VARCHAR(255) NOT NULL DEFAULT '',
    prompt_version VARCHAR(100) NOT NULL DEFAULT '',
    raw_response LONGTEXT NOT NULL DEFAULT (''),
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    analyzed_at VARCHAR(40) NULL,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (article_id),
    KEY idx_article_analyses_relevance (status, is_relevant, category),
    CONSTRAINT fk_article_analyses_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT chk_article_analyses_status CHECK (
        status IN ('processing', 'success', 'failed')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS article_contents (
    article_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    requested_url TEXT NOT NULL DEFAULT (''),
    final_url TEXT NOT NULL DEFAULT (''),
    full_text LONGTEXT NOT NULL DEFAULT (''),
    content_hash CHAR(64) NOT NULL DEFAULT '',
    content_chars INT NOT NULL DEFAULT 0,
    http_status INT NOT NULL DEFAULT 0,
    content_type VARCHAR(255) NOT NULL DEFAULT '',
    extractor VARCHAR(100) NOT NULL DEFAULT '',
    fetched_at VARCHAR(40) NULL,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    attempt_count INT NOT NULL DEFAULT 0,
    failure_kind VARCHAR(100) NOT NULL DEFAULT '',
    next_retry_at VARCHAR(40) NULL,
    is_terminal TINYINT NOT NULL DEFAULT 0,
    ignored_at VARCHAR(40) NULL,
    PRIMARY KEY (article_id),
    KEY idx_article_contents_status (status, fetched_at),
    CONSTRAINT fk_article_contents_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT chk_article_contents_status CHECK (
        status IN ('processing', 'success', 'failed')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS article_relevance_reviews (
    article_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_relevant TINYINT NOT NULL DEFAULT 0,
    relevance_score INT NOT NULL DEFAULT 0,
    relevance_reason LONGTEXT NOT NULL DEFAULT (''),
    category VARCHAR(100) NOT NULL DEFAULT 'other',
    secondary_categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    keyword_categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    evidence JSON NOT NULL DEFAULT (JSON_ARRAY()),
    confidence INT NOT NULL DEFAULT 0,
    content_hash CHAR(64) NOT NULL DEFAULT '',
    model VARCHAR(255) NOT NULL DEFAULT '',
    prompt_version VARCHAR(100) NOT NULL DEFAULT '',
    raw_response LONGTEXT NOT NULL DEFAULT (''),
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    reviewed_at VARCHAR(40) NULL,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (article_id),
    KEY idx_article_relevance_reviews_result (status, is_relevant, relevance_score),
    CONSTRAINT fk_relevance_reviews_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT chk_relevance_reviews_status CHECK (
        status IN ('processing', 'success', 'failed')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS business_articles (
    article_id BIGINT NOT NULL,
    analysis_status VARCHAR(16) NOT NULL,
    relevance_score INT NOT NULL,
    relevance_reason LONGTEXT NOT NULL,
    relevance_confidence INT NOT NULL DEFAULT 0,
    relevance_evidence JSON NOT NULL DEFAULT (JSON_ARRAY()),
    category VARCHAR(100) NOT NULL DEFAULT 'other',
    secondary_categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    summary LONGTEXT NOT NULL DEFAULT (''),
    impact_direction VARCHAR(20) NOT NULL DEFAULT 'neutral',
    impact_score INT NOT NULL DEFAULT 1,
    impact_analysis LONGTEXT NOT NULL DEFAULT (''),
    risk_level VARCHAR(20) NOT NULL DEFAULT 'low',
    risk_score INT NOT NULL DEFAULT 0,
    risk_factors JSON NOT NULL DEFAULT (JSON_ARRAY()),
    opportunities JSON NOT NULL DEFAULT (JSON_ARRAY()),
    recommended_actions JSON NOT NULL DEFAULT (JSON_ARRAY()),
    analysis_evidence JSON NOT NULL DEFAULT (JSON_ARRAY()),
    content_hash CHAR(64) NOT NULL,
    model VARCHAR(255) NOT NULL DEFAULT '',
    prompt_version VARCHAR(100) NOT NULL DEFAULT '',
    raw_response LONGTEXT NOT NULL DEFAULT (''),
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    accepted_at VARCHAR(40) NOT NULL,
    analyzed_at VARCHAR(40) NULL,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (article_id),
    KEY idx_business_articles_analysis (
        analysis_status, category, risk_score DESC
    ),
    CONSTRAINT fk_business_articles_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT chk_business_articles_status CHECK (
        analysis_status IN ('processing', 'success', 'failed')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS ai_analysis_run_items (
    run_id BIGINT NOT NULL,
    article_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_relevant TINYINT NOT NULL DEFAULT 0,
    content_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    relevance_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    business_analysis_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (run_id, article_id),
    KEY idx_ai_run_items_article_id (article_id),
    CONSTRAINT fk_ai_run_items_run FOREIGN KEY (run_id)
        REFERENCES ai_analysis_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_run_items_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE,
    CONSTRAINT chk_ai_run_items_status CHECK (
        status IN ('pending', 'processing', 'success', 'failed')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS daily_reports (
    id BIGINT NOT NULL AUTO_INCREMENT,
    report_date CHAR(10) NOT NULL,
    categories JSON NOT NULL DEFAULT (JSON_ARRAY()),
    keyword_category_id BIGINT NULL,
    keyword_category_name VARCHAR(191) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL,
    risk_level VARCHAR(20) NOT NULL DEFAULT 'low',
    risk_score INT NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT (''),
    executive_summary LONGTEXT NOT NULL DEFAULT (''),
    risk_basis LONGTEXT NOT NULL DEFAULT (''),
    key_developments JSON NOT NULL DEFAULT (JSON_ARRAY()),
    key_risks JSON NOT NULL DEFAULT (JSON_ARRAY()),
    opportunities JSON NOT NULL DEFAULT (JSON_ARRAY()),
    recommended_actions JSON NOT NULL DEFAULT (JSON_ARRAY()),
    watchlist JSON NOT NULL DEFAULT (JSON_ARRAY()),
    article_count INT NOT NULL DEFAULT 0,
    model VARCHAR(255) NOT NULL,
    prompt_version VARCHAR(100) NOT NULL,
    raw_response LONGTEXT NOT NULL DEFAULT (''),
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    created_at VARCHAR(40) NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    error_message LONGTEXT NOT NULL DEFAULT (''),
    feishu_status VARCHAR(20) NOT NULL DEFAULT 'not_pushed',
    feishu_pushed_at VARCHAR(40) NULL,
    feishu_error_message LONGTEXT NOT NULL DEFAULT (''),
    PRIMARY KEY (id),
    KEY idx_daily_reports_date (report_date DESC, id DESC),
    KEY idx_daily_reports_keyword_category (
        report_date DESC, keyword_category_id, status, id DESC
    ),
    CONSTRAINT fk_daily_reports_keyword_category FOREIGN KEY (keyword_category_id)
        REFERENCES keyword_categories(id) ON DELETE SET NULL,
    CONSTRAINT chk_daily_reports_status CHECK (
        status IN ('running', 'success', 'failed', 'interrupted')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS daily_report_articles (
    report_id BIGINT NOT NULL,
    article_id BIGINT NOT NULL,
    PRIMARY KEY (report_id, article_id),
    KEY idx_daily_report_articles_article_id (article_id),
    CONSTRAINT fk_daily_report_articles_report FOREIGN KEY (report_id)
        REFERENCES daily_reports(id) ON DELETE CASCADE,
    CONSTRAINT fk_daily_report_articles_article FOREIGN KEY (article_id)
        REFERENCES articles(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS app_settings (
    `key` VARCHAR(191) NOT NULL,
    value LONGTEXT NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""


class CompatRow(dict[str, Any]):
    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class MySQLCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> CompatRow | None:
        row = self._cursor.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self) -> list[CompatRow]:
        return [CompatRow(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size: int = 1000) -> list[CompatRow]:
        return [CompatRow(row) for row in self._cursor.fetchmany(size)]

    def __iter__(self) -> Iterator[CompatRow]:
        for row in self._cursor:
            yield CompatRow(row)

    def close(self) -> None:
        self._cursor.close()


class EmptyCursor:
    lastrowid = None
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __iter__(self) -> Iterator[Any]:
        return iter(())


def _first_insert_column(sql: str) -> str:
    match = re.search(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+[`\w]+\s*\(([^)]+)\)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "id"
    return match.group(1).split(",", 1)[0].strip()


def convert_sqlite_sql_to_mysql(sql: str) -> str:
    converted = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT IGNORE INTO",
        sql,
        flags=re.IGNORECASE,
    )
    converted = re.sub(
        r"datetime\(([^()]+)\)\s*([<>]=?)\s*datetime\('now'\)",
        (
            r"\1 \2 DATE_FORMAT(UTC_TIMESTAMP(6), "
            r"'%Y-%m-%dT%H:%i:%s.%fZ')"
        ),
        converted,
        flags=re.IGNORECASE,
    )

    conflict = re.search(
        r"\s+ON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+(NOTHING|UPDATE\s+SET)",
        converted,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if conflict:
        if conflict.group(2).upper() == "NOTHING":
            column = _first_insert_column(converted)
            replacement = f" ON DUPLICATE KEY UPDATE {column} = {column}"
        else:
            replacement = " ON DUPLICATE KEY UPDATE"
        converted = converted[: conflict.start()] + replacement + converted[conflict.end() :]
        converted = re.sub(
            r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)",
            r"VALUES(\1)",
            converted,
            flags=re.IGNORECASE,
        )

    return converted.replace("?", "%s")


def escape_parameterized_percent_literals(sql: str) -> str:
    """Escape percent signs in SQL literals while preserving %s placeholders."""
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            if character == "%":
                result.append("%%")
            else:
                result.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
            elif character == "\\" and index + 1 < len(sql):
                result.append(sql[index + 1])
                index += 1
        else:
            if character in {"'", '"', "`"}:
                quote = character
                result.append(character)
            elif character == "%":
                if index + 1 < len(sql) and sql[index + 1] == "s":
                    result.append("%s")
                    index += 1
                else:
                    result.append("%%")
            else:
                result.append(character)
        index += 1
    return "".join(result)


def _split_schema(schema: str) -> list[str]:
    return [statement.strip() for statement in schema.split(";") if statement.strip()]


class MySQLConnection:
    def __init__(self, settings: MySQLSettings) -> None:
        if pymysql is None or CLIENT is None or DictCursor is None:
            raise DatabaseBackendError(
                "缺少 PyMySQL，请先执行 pip install -r requirements.txt"
            )
        try:
            self._connection = pymysql.connect(
                host=settings.host,
                port=settings.port,
                user=settings.user,
                password=settings.password,
                database=settings.database,
                charset=settings.charset,
                cursorclass=DictCursor,
                autocommit=False,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
                client_flag=CLIENT.FOUND_ROWS,
                init_command="SET time_zone = '+00:00'",
            )
        except pymysql.MySQLError as exc:
            raise DatabaseBackendError(f"无法连接 MySQL：{exc}") from exc
        self.database = settings.database

    def _run(self, sql: str, parameters: Sequence[Any] | None = None) -> MySQLCursor:
        cursor = self._connection.cursor()
        parameter_values = tuple(parameters) if parameters else None
        if parameter_values is not None:
            sql = escape_parameterized_percent_literals(sql)
        try:
            cursor.execute(sql, parameter_values)
        except pymysql.err.IntegrityError as exc:
            cursor.close()
            raise DatabaseIntegrityError(str(exc)) from exc
        except pymysql.MySQLError as exc:
            cursor.close()
            raise DatabaseBackendError(str(exc)) from exc
        return MySQLCursor(cursor)

    def execute(
        self, sql: str, parameters: Sequence[Any] | None = None
    ) -> MySQLCursor | EmptyCursor:
        pragma = re.fullmatch(
            r"\s*PRAGMA\s+table_info\(([^)]+)\)\s*;?\s*",
            sql,
            flags=re.IGNORECASE,
        )
        if pragma:
            table = pragma.group(1).strip("`\"' ")
            return self._run(
                """
                SELECT COLUMN_NAME AS name
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self.database, table),
            )

        create_index = re.match(
            r"\s*CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if create_index:
            index_name = create_index.group(2)
            table_name = create_index.group(3)
            exists = self._run(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
                LIMIT 1
                """,
                (self.database, table_name, index_name),
            ).fetchone()
            if exists:
                return EmptyCursor()
            sql = re.sub(
                r"\bIF\s+NOT\s+EXISTS\s+",
                "",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )

        converted = convert_sqlite_sql_to_mysql(sql)
        return self._run(converted, parameters)

    def executemany(
        self, sql: str, parameter_rows: Iterable[Sequence[Any]]
    ) -> MySQLCursor:
        cursor = self._connection.cursor()
        rows = [tuple(row) for row in parameter_rows]
        converted = convert_sqlite_sql_to_mysql(sql)
        if rows:
            converted = escape_parameterized_percent_literals(converted)
        try:
            cursor.executemany(converted, rows)
        except pymysql.err.IntegrityError as exc:
            cursor.close()
            raise DatabaseIntegrityError(str(exc)) from exc
        except pymysql.MySQLError as exc:
            cursor.close()
            raise DatabaseBackendError(str(exc)) from exc
        return MySQLCursor(cursor)

    def executescript(self, _sqlite_schema: str) -> None:
        for statement in _split_schema(MYSQL_SCHEMA):
            self._run(statement).close()

    def escape(self, value: Any) -> str:
        return self._connection.escape(value)

    def commit(self) -> None:
        try:
            self._connection.commit()
        except pymysql.MySQLError as exc:
            raise DatabaseBackendError(str(exc)) from exc

    def rollback(self) -> None:
        try:
            self._connection.rollback()
        except pymysql.MySQLError as exc:
            raise DatabaseBackendError(str(exc)) from exc

    def close(self) -> None:
        self._connection.close()


def dump_mysql_database(connection: MySQLConnection, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n")
        for table in MYSQL_TABLES:
            create_row = connection.execute(f"SHOW CREATE TABLE `{table}`").fetchone()
            if create_row is None:
                continue
            create_sql = create_row[1]
            output.write(f"DROP TABLE IF EXISTS `{table}`;\n{create_sql};\n\n")

            column_rows = connection.execute(f"SHOW COLUMNS FROM `{table}`").fetchall()
            columns = [
                row["Field"]
                for row in column_rows
                if "GENERATED" not in str(row.get("Extra", "")).upper()
            ]
            if not columns:
                continue
            column_sql = ", ".join(f"`{column}`" for column in columns)
            cursor = connection.execute(f"SELECT {column_sql} FROM `{table}`")
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    values = ", ".join(connection.escape(row[column]) for column in columns)
                    output.write(
                        f"INSERT INTO `{table}` ({column_sql}) VALUES ({values});\n"
                    )
            output.write("\n")
        output.write("SET FOREIGN_KEY_CHECKS=1;\n")
