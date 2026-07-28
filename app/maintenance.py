from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal

from .database import Database


CleanupScope = Literal["failed_records", "history", "all_collected"]
VALID_CLEANUP_SCOPES = {"failed_records", "history", "all_collected"}


class CleanupError(RuntimeError):
    pass


class CleanupBusyError(CleanupError):
    pass


class CleanupConfirmationError(CleanupError):
    pass


class CleanupService:
    """Preview, back up, and remove collected data through one guarded seam."""

    def __init__(
        self,
        database: Database,
        *,
        backup_dir: Path,
        is_busy: Callable[[], bool],
    ) -> None:
        self.database = database
        self.backup_dir = Path(backup_dir)
        self.is_busy = is_busy

    def preview(
        self, scope: CleanupScope | str, *, before: str | None = None
    ) -> dict[str, object]:
        normalized_scope, normalized_before = self._validate(scope, before)
        conditions, parameters = self._conditions(
            normalized_scope, normalized_before
        )
        with self.database.connect() as connection:
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} {conditions[table]}",
                        parameters[table],
                    ).fetchone()[0]
                )
                for table in (
                    "articles",
                    "collection_runs",
                    "ai_analysis_runs",
                    "daily_reports",
                )
            }
        return {
            "scope": normalized_scope,
            "before": normalized_before,
            **counts,
            "total_records": sum(counts.values()),
        }

    def execute(
        self,
        scope: CleanupScope | str,
        *,
        confirmation: str,
        before: str | None = None,
    ) -> dict[str, object]:
        if confirmation != "DELETE":
            raise CleanupConfirmationError("请输入 DELETE 确认清理")
        if self.is_busy():
            raise CleanupBusyError("采集、AI 分析或日报任务运行中，不能清理数据")

        preview = self.preview(scope, before=before)
        backup_path: Path | None = None
        if int(preview["total_records"]) > 0:
            backup_path = self._create_backup()
            normalized_scope = str(preview["scope"])
            normalized_before = preview["before"]
            conditions, parameters = self._conditions(
                normalized_scope, str(normalized_before) if normalized_before else None
            )
            with self.database.connect() as connection:
                for table in (
                    "daily_reports",
                    "ai_analysis_runs",
                    "collection_runs",
                    "articles",
                ):
                    connection.execute(
                        f"DELETE FROM {table} {conditions[table]}",
                        parameters[table],
                    )

        return {
            "scope": preview["scope"],
            "before": preview["before"],
            "deleted": {
                key: preview[key]
                for key in (
                    "articles",
                    "collection_runs",
                    "ai_analysis_runs",
                    "daily_reports",
                )
            },
            "backup_path": str(backup_path.resolve()) if backup_path else None,
        }

    def _create_backup(self) -> Path:
        return self.database.create_backup(self.backup_dir)

    @staticmethod
    def _validate(
        scope: CleanupScope | str, before: str | None
    ) -> tuple[str, str | None]:
        if scope not in VALID_CLEANUP_SCOPES:
            raise CleanupError("未知数据清理范围")
        normalized_before: str | None = None
        if before:
            try:
                normalized_before = date.fromisoformat(before).isoformat()
            except ValueError as exc:
                raise CleanupError("历史数据截止日期格式必须为 YYYY-MM-DD") from exc
        if scope == "history" and normalized_before is None:
            raise CleanupError("清理历史数据必须指定截止日期")
        return str(scope), normalized_before

    @staticmethod
    def _conditions(
        scope: str, before: str | None
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        tables = (
            "articles",
            "collection_runs",
            "ai_analysis_runs",
            "daily_reports",
        )
        if scope == "all_collected":
            return ({table: "" for table in tables}, {table: () for table in tables})
        if scope == "failed_records":
            return (
                {
                    "articles": (
                        "WHERE id IN (SELECT article_id FROM article_contents "
                        "WHERE status = 'failed')"
                    ),
                    "collection_runs": (
                        "WHERE status IN ('partial', 'failed', 'interrupted')"
                    ),
                    "ai_analysis_runs": (
                        "WHERE status IN ('partial', 'failed', 'interrupted')"
                    ),
                    "daily_reports": "WHERE status IN ('failed', 'interrupted')",
                },
                {table: () for table in tables},
            )

        assert before is not None
        cutoff = f"{before}T00:00:00Z"
        return (
            {
                "articles": "WHERE published_at < ?",
                "collection_runs": (
                    "WHERE status <> 'running' AND started_at < ?"
                ),
                "ai_analysis_runs": (
                    "WHERE status <> 'running' AND started_at < ?"
                ),
                "daily_reports": "WHERE report_date < ?",
            },
            {
                "articles": (cutoff,),
                "collection_runs": (cutoff,),
                "ai_analysis_runs": (cutoff,),
                "daily_reports": (before,),
            },
        )
