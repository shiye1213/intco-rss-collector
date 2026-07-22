from __future__ import annotations

from pathlib import Path

import pytest

from app.database import Database
from app.maintenance import (
    CleanupBusyError,
    CleanupConfirmationError,
    CleanupService,
)


def insert_article(database: Database, *, published_at: str) -> int:
    slug = published_at.replace(":", "-")
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO articles
                (title, url, canonical_url, fingerprint, publisher,
                 publisher_normalized, summary, published_at, collected_at)
            VALUES
                ('待清理文章', ?, ?, ?, 'Example', 'Example', '', ?, ?)
            """,
            (
                f"https://example.com/cleanup/{slug}",
                f"https://example.com/cleanup/{slug}",
                f"cleanup-{slug}",
                published_at,
                published_at,
            ),
        )
        return int(cursor.lastrowid)


def test_cleanup_requires_confirmation_and_refuses_while_task_is_running(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "maintenance.db")
    database.initialize()
    insert_article(database, published_at="2026-01-01T00:00:00Z")
    service = CleanupService(
        database,
        backup_dir=tmp_path / "backups",
        is_busy=lambda: True,
    )

    preview = service.preview("all_collected")
    assert preview["articles"] == 1
    with pytest.raises(CleanupConfirmationError):
        service.execute("all_collected", confirmation="wrong")
    with pytest.raises(CleanupBusyError):
        service.execute("all_collected", confirmation="DELETE")
    assert database.article_count() == 1
    assert list((tmp_path / "backups").glob("*.db")) == []


def test_cleanup_creates_backup_then_deletes_selected_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "history.db")
    database.initialize()
    old_id = insert_article(database, published_at="2026-01-01T00:00:00Z")
    new_id = insert_article(database, published_at="2026-07-20T00:00:00Z")
    service = CleanupService(
        database,
        backup_dir=tmp_path / "backups",
        is_busy=lambda: False,
    )

    preview = service.preview("history", before="2026-07-01")
    assert preview["articles"] == 1
    result = service.execute(
        "history", before="2026-07-01", confirmation="DELETE"
    )

    assert result["deleted"]["articles"] == 1
    backup_path = Path(result["backup_path"])
    assert backup_path.is_file()
    backup_database = Database(backup_path)
    assert backup_database.article_count() == 2
    with database.connect() as connection:
        remaining = {
            row[0] for row in connection.execute("SELECT id FROM articles")
        }
    assert old_id not in remaining
    assert new_id in remaining
