from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from app.mysql_backend import MYSQL_TABLES  # noqa: E402

CONFIRMATION = "REPLACE_MYSQL"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy the existing SQLite data into an initialized MySQL database."
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=ROOT / "data" / "rss_collector.db",
        help="Path to the existing SQLite database.",
    )
    parser.add_argument(
        "--mysql-url",
        default=None,
        help="MySQL URL; defaults to DATABASE_URL from .env or the environment.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required safety confirmation: {CONFIRMATION}",
    )
    return parser.parse_args()


def source_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def mysql_columns(connection: Any, table: str) -> tuple[list[str], set[str]]:
    rows = connection.execute(f"SHOW COLUMNS FROM `{table}`").fetchall()
    columns = [
        str(row["Field"])
        for row in rows
        if "GENERATED" not in str(row.get("Extra", "")).upper()
    ]
    json_columns = {
        str(row["Field"])
        for row in rows
        if str(row.get("Type", "")).lower() == "json"
    }
    return columns, json_columns


def normalize_json(value: Any) -> str:
    if value is None or value == "":
        return "[]"
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return "[]"
    return value


def migrate(sqlite_path: Path, mysql_url: str) -> dict[str, int]:
    source = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = Database(mysql_url)
    if target.backend != "mysql":
        raise ValueError("--mysql-url must use mysql:// or mysql+pymysql://")
    target.initialize()
    backup_path = target.create_backup(ROOT / "data" / "backups")
    print(f"Target backup created: {backup_path}")

    migrated: dict[str, int] = {}
    available_tables = source_tables(source)
    try:
        with target.connect() as destination:
            destination.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                for table in reversed(MYSQL_TABLES):
                    destination.execute(f"DELETE FROM `{table}`")

                for table in MYSQL_TABLES:
                    if table not in available_tables:
                        migrated[table] = 0
                        continue
                    target_columns, json_columns = mysql_columns(destination, table)
                    source_column_rows = source.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                    source_columns = {str(row[1]) for row in source_column_rows}
                    columns = [
                        column for column in target_columns if column in source_columns
                    ]
                    if not columns:
                        migrated[table] = 0
                        continue
                    quoted_columns = ", ".join(f"`{column}`" for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    insert_sql = (
                        f"INSERT INTO `{table}` ({quoted_columns}) "
                        f"VALUES ({placeholders})"
                    )
                    cursor = source.execute(
                        f"SELECT {quoted_columns} FROM `{table}`"
                    )
                    count = 0
                    while True:
                        rows = cursor.fetchmany(500)
                        if not rows:
                            break
                        values = [
                            tuple(
                                normalize_json(row[column])
                                if column in json_columns
                                else row[column]
                                for column in columns
                            )
                            for row in rows
                        ]
                        destination.executemany(insert_sql, values)
                        count += len(values)
                    migrated[table] = count
            finally:
                destination.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        source.close()
    return migrated


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    sqlite_path = args.sqlite.resolve()
    mysql_url = args.mysql_url or os.getenv("DATABASE_URL", "")
    if args.confirm != CONFIRMATION:
        print(
            f"Refusing to replace the target database. Pass --confirm {CONFIRMATION}.",
            file=sys.stderr,
        )
        return 2
    if not sqlite_path.is_file():
        print(f"SQLite database not found: {sqlite_path}", file=sys.stderr)
        return 2
    if not mysql_url:
        print("DATABASE_URL or --mysql-url is required.", file=sys.stderr)
        return 2

    migrated = migrate(sqlite_path, mysql_url)
    total = sum(migrated.values())
    print(f"Migration complete: {total} rows")
    for table, count in migrated.items():
        print(f"  {table}: {count}")
    print(f"SQLite source preserved: {sqlite_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
