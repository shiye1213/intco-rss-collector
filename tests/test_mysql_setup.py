from typing import Any

import pytest

from app.mysql_backend import MySQLSettings
from scripts import setup_mysql


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parameters: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, parameters))


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_bootstrap_server_creates_database_and_local_accounts(monkeypatch) -> None:
    fake = FakeConnection()
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> FakeConnection:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(setup_mysql.pymysql, "connect", fake_connect)
    settings = MySQLSettings(
        host="127.0.0.1",
        port=3306,
        user="rss_collector",
        password="app-secret",
        database="rss_collector",
    )

    setup_mysql.bootstrap_server(
        settings,
        root_user="root",
        root_password="root-secret",
    )

    assert captured["user"] == "root"
    assert captured["password"] == "root-secret"
    assert fake.committed is True
    assert fake.rolled_back is False
    assert fake.closed is True
    assert len(fake.cursor_instance.calls) == 7
    assert fake.cursor_instance.calls[0][0].startswith(
        "CREATE DATABASE IF NOT EXISTS `rss_collector`"
    )
    assert fake.cursor_instance.calls[1][1] == (
        "rss_collector",
        "localhost",
        "app-secret",
    )
    assert "app-secret" not in fake.cursor_instance.calls[1][0]


def test_bootstrap_server_rejects_unsafe_account_name(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_mysql.pymysql,
        "connect",
        lambda **_: pytest.fail("connection should not be attempted"),
    )
    settings = MySQLSettings(
        host="127.0.0.1",
        port=3306,
        user="unsafe'user",
        password="secret",
        database="rss_collector",
    )

    with pytest.raises(ValueError):
        setup_mysql.bootstrap_server(
            settings,
            root_user="root",
            root_password="root-secret",
        )
