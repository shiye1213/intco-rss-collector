from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from app.mysql_backend import (  # noqa: E402
    DatabaseBackendError,
    MYSQL_TABLES,
    MySQLSettings,
    parse_mysql_url,
)

APP_ACCOUNT_HOSTS = ("localhost", "127.0.0.1")
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9_$-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the MySQL database, application user, tables, and defaults."
    )
    parser.add_argument(
        "--root-user",
        default="root",
        help="MySQL administrator account used for one-time setup.",
    )
    return parser.parse_args()


def validate_account_name(value: str, label: str) -> str:
    if not ACCOUNT_PATTERN.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters: {value!r}")
    return value


def bootstrap_server(
    settings: MySQLSettings,
    *,
    root_user: str,
    root_password: str,
) -> None:
    root_user = validate_account_name(root_user, "root user")
    app_user = validate_account_name(settings.user, "application user")
    connection = pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=root_user,
        password=root_password,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            for account_host in APP_ACCOUNT_HOSTS:
                cursor.execute(
                    "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                    (app_user, account_host, settings.password),
                )
                cursor.execute(
                    "ALTER USER %s@%s IDENTIFIED BY %s",
                    (app_user, account_host, settings.password),
                )
                cursor.execute(
                    f"GRANT ALL PRIVILEGES ON `{settings.database}`.* TO %s@%s",
                    (app_user, account_host),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_tables(database_url: str, database_name: str) -> int:
    database = Database(database_url)
    database.initialize()
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS table_count
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
            """,
            (database_name,),
        ).fetchone()
    return int(row["table_count"] if row else 0)


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is missing from .env.", file=sys.stderr)
        return 2

    try:
        settings = parse_mysql_url(database_url)
        print(
            "Target: "
            f"{settings.user}@{settings.host}:{settings.port}/{settings.database}"
        )
        root_password = getpass.getpass("MySQL root password: ")
        bootstrap_server(
            settings,
            root_user=args.root_user,
            root_password=root_password,
        )
        table_count = initialize_tables(database_url, settings.database)
    except (ValueError, pymysql.MySQLError, DatabaseBackendError) as exc:
        print(f"MySQL setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"MySQL setup complete: {table_count} tables are available.")
    print(f"Expected application tables: {len(MYSQL_TABLES)}")
    print("Existing table data was preserved; no DROP or DELETE was executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
