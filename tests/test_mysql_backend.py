from pathlib import Path

import pytest

from app.database import Database, KEYWORD_CATEGORY_RELEVANCE_SQL
from app.mysql_backend import (
    MYSQL_SCHEMA,
    MYSQL_TABLES,
    convert_sqlite_sql_to_mysql,
    escape_parameterized_percent_literals,
    parse_mysql_url,
)


def test_parse_mysql_url_supports_encoded_credentials() -> None:
    settings = parse_mysql_url(
        "mysql+pymysql://rss%40user:p%40ss@db.example:3307/intelligence?charset=utf8mb4"
    )

    assert settings.host == "db.example"
    assert settings.port == 3307
    assert settings.user == "rss@user"
    assert settings.password == "p@ss"
    assert settings.database == "intelligence"
    assert settings.charset == "utf8mb4"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///data.db",
        "mysql://localhost/rss_collector",
        "mysql://user@localhost/invalid.name",
        "mysql://user@localhost/rss_collector?charset=utf8mb4%20bad",
    ],
)
def test_parse_mysql_url_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_mysql_url(url)


def test_database_selects_backend_from_target(tmp_path: Path) -> None:
    sqlite_database = Database(tmp_path / "local.db")
    mysql_database = Database(
        "mysql://rss_collector:secret@127.0.0.1:3306/rss_collector"
    )

    assert sqlite_database.backend == "sqlite"
    assert sqlite_database.path == tmp_path / "local.db"
    assert mysql_database.backend == "mysql"
    assert mysql_database.path is None


def test_sql_converter_handles_qmark_ignore_and_upsert() -> None:
    ignored = convert_sqlite_sql_to_mysql(
        "INSERT OR IGNORE INTO daily_report_articles "
        "(report_id, article_id) VALUES (?, ?)"
    )
    upsert = convert_sqlite_sql_to_mysql(
        "INSERT INTO app_settings (`key`, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(`key`) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at"
    )

    assert ignored.startswith("INSERT IGNORE INTO")
    assert ignored.count("%s") == 2
    assert "ON DUPLICATE KEY UPDATE" in upsert
    assert "value = VALUES(value)" in upsert
    assert "updated_at = VALUES(updated_at)" in upsert
    assert upsert.count("%s") == 3


def test_sql_converter_handles_do_nothing_and_utc_datetime() -> None:
    converted = convert_sqlite_sql_to_mysql(
        "INSERT INTO app_settings (`key`, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(`key`) DO NOTHING"
    )
    retry_query = convert_sqlite_sql_to_mysql(
        "SELECT 1 WHERE datetime(next_retry_at) <= datetime('now')"
    )

    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in converted
    assert "UTC_TIMESTAMP(6)" in retry_query
    assert "datetime(" not in retry_query


def test_parameterized_sql_escapes_literal_percent_signs() -> None:
    converted = convert_sqlite_sql_to_mysql(
        "SELECT 1 WHERE datetime(next_retry_at) <= datetime('now') LIMIT ?"
    )
    prepared = escape_parameterized_percent_literals(converted)

    assert "%s" in prepared
    assert "'%%Y-%%m-%%dT%%H:%%i:%%s.%%fZ'" in prepared


def test_parameterized_sql_escapes_like_wildcards() -> None:
    prepared = escape_parameterized_percent_literals(
        "SELECT 1 WHERE title LIKE '%sports%' AND id = %s"
    )

    assert "LIKE '%%sports%%'" in prepared
    assert prepared.endswith("id = %s")


def test_mysql_schema_covers_all_application_tables() -> None:
    for table in MYSQL_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in MYSQL_SCHEMA
    assert "ENGINE=InnoDB" in MYSQL_SCHEMA
    assert "CHARSET=utf8mb4" in MYSQL_SCHEMA
    assert "AUTOINCREMENT" not in MYSQL_SCHEMA


def test_keyword_category_filter_is_portable() -> None:
    assert "json_each" not in KEYWORD_CATEGORY_RELEVANCE_SQL
    assert "json_valid" not in KEYWORD_CATEGORY_RELEVANCE_SQL
    assert "keyword_categories LIKE" in KEYWORD_CATEGORY_RELEVANCE_SQL
