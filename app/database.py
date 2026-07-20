from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

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
    active INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    query TEXT NOT NULL,
    match_terms TEXT NOT NULL,
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
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    rss_source_id INTEGER REFERENCES rss_sources(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_canonical_url
ON articles(canonical_url) WHERE canonical_url IS NOT NULL AND canonical_url <> '';
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_collected_at ON articles(collected_at DESC);

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

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


DEFAULT_SOURCES = (
    {
        "name": "Google News 中文",
        "url_template": (
            "https://news.google.com/rss/search?q={query}"
            "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        ),
        "mode": "search",
        "language": "zh-CN",
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
        "active": True,
    },
    {
        "name": "欧盟医疗器械标准",
        "url_template": "https://ec.europa.eu/newsroom/growth/feed?tpa_id=30111",
        "mode": "direct",
        "language": "en",
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
        "active": True,
    },
    {
        "name": "巴西 ANVISA",
        "url_template": "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/RSS",
        "mode": "direct",
        "language": "pt-BR",
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
        "active": True,
    },
    {
        "name": "ECDC 传染病威胁",
        "url_template": "https://www.ecdc.europa.eu/en/taxonomy/term/1505/feed",
        "mode": "direct",
        "language": "en",
        "active": True,
    },
    {
        "name": "欧盟贸易新闻",
        "url_template": "https://policy.trade.ec.europa.eu/node/2/rss_en",
        "mode": "direct",
        "language": "en",
        "active": True,
    },
    {
        "name": "WTO 新闻",
        "url_template": "https://www.wto.org/library/rss/latest_news_e.xml",
        "mode": "direct",
        "language": "en",
        "active": True,
    },
)

DEFAULT_KEYWORDS = (
    {
        "name": "PE 手套",
        "match_terms": [
            "PE手套",
            "聚乙烯手套",
            "一次性PE手套",
            "polyethylene gloves",
            "disposable PE gloves",
            "PE disposable gloves",
        ],
        "active": True,
    },
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
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
            source_count = connection.execute(
                "SELECT COUNT(*) FROM rss_sources"
            ).fetchone()[0]
            if source_count == 0:
                for source in DEFAULT_SOURCES:
                    connection.execute(
                        """
                        INSERT INTO rss_sources
                            (name, url_template, mode, language, active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source["name"],
                            source["url_template"],
                            source["mode"],
                            source["language"],
                            int(source["active"]),
                            now,
                            now,
                        ),
                    )
            keyword_count = connection.execute(
                "SELECT COUNT(*) FROM keywords"
            ).fetchone()[0]
            if keyword_count == 0:
                for keyword in DEFAULT_KEYWORDS:
                    connection.execute(
                        """
                        INSERT INTO keywords
                            (name, query, match_terms, active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            keyword["name"],
                            build_keyword_query(keyword["match_terms"]),
                            json.dumps(keyword["match_terms"], ensure_ascii=False),
                            int(keyword["active"]),
                            now,
                            now,
                        ),
                    )
            keyword_rows = connection.execute(
                "SELECT id, query, match_terms FROM keywords"
            ).fetchall()
            for keyword_row in keyword_rows:
                try:
                    generated_query = build_keyword_query(
                        json.loads(keyword_row["match_terms"])
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
            }
            for key, value in defaults.items():
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, value, now),
                )

    @staticmethod
    def rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_sources(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE archived = 0"
        if active_only:
            where += " AND active = 1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM rss_sources {where} ORDER BY id"  # noqa: S608
            ).fetchall()
        return self.rows(rows)

    def get_keywords(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE archived = 0"
        if active_only:
            where += " AND active = 1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM keywords {where} ORDER BY id"  # noqa: S608
            ).fetchall()
        result = self.rows(rows)
        for item in result:
            item["match_terms"] = json.loads(item["match_terms"])
        return result

    def create_source(self, data: dict[str, Any]) -> int:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rss_sources
                    (name, url_template, mode, language, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    data["url_template"],
                    data["mode"],
                    data.get("language", ""),
                    int(data.get("active", True)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_source(self, source_id: int, data: dict[str, Any]) -> bool:
        now = utc_now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE rss_sources
                SET name = ?, url_template = ?, mode = ?, language = ?,
                    active = ?, updated_at = ?
                WHERE id = ? AND archived = 0
                """,
                (
                    data["name"],
                    data["url_template"],
                    data["mode"],
                    data.get("language", ""),
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
        query = build_keyword_query(data["match_terms"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO keywords
                    (name, query, match_terms, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    query,
                    json.dumps(data["match_terms"], ensure_ascii=False),
                    int(data.get("active", True)),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_keyword(self, keyword_id: int, data: dict[str, Any]) -> bool:
        now = utc_now_iso()
        query = build_keyword_query(data["match_terms"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE keywords
                SET name = ?, query = ?, match_terms = ?, active = ?, updated_at = ?
                WHERE id = ? AND archived = 0
                """,
                (
                    data["name"],
                    query,
                    json.dumps(data["match_terms"], ensure_ascii=False),
                    int(data.get("active", True)),
                    now,
                    keyword_id,
                ),
            )
            return cursor.rowcount > 0

    def archive_keyword(self, keyword_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE keywords SET active = 0, archived = 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), keyword_id),
            )
            return cursor.rowcount > 0

    def get_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
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
            filters.append("(a.title LIKE ? OR a.summary LIKE ? OR a.publisher LIKE ?)")
            term = f"%{query}%"
            parameters.extend((term, term, term))
        if source_id is not None:
            filters.append("a.rss_source_id = ?")
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
                SELECT a.*, s.name AS feed_name,
                       GROUP_CONCAT(DISTINCT k.name) AS keyword_names
                FROM articles a
                LEFT JOIN rss_sources s ON s.id = a.rss_source_id
                LEFT JOIN article_keywords ak ON ak.article_id = a.id
                LEFT JOIN keywords k ON k.id = ak.keyword_id
                {where}
                GROUP BY a.id
                ORDER BY a.published_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                [*parameters, limit, offset],
            ).fetchall()
        return {"total": int(total), "items": self.rows(rows)}

    def article_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
