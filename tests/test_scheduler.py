from datetime import datetime
from zoneinfo import ZoneInfo

from app.database import Database
from app.scheduler import next_scheduled_at, parse_schedule_time


def test_parse_schedule_time() -> None:
    assert parse_schedule_time("09:30").hour == 9
    assert parse_schedule_time("09:30").minute == 30
    assert parse_schedule_time("invalid").hour == 8


def test_next_schedule_is_today_before_schedule(tmp_path) -> None:
    database = Database(tmp_path / "schedule.db")
    database.initialize()
    database.set_setting("schedule_time", "08:00")
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 20, 7, 0, tzinfo=timezone)

    result = next_scheduled_at(database, now)

    assert result == datetime(2026, 7, 20, 8, 0, tzinfo=timezone)


def test_next_schedule_is_tomorrow_after_schedule(tmp_path) -> None:
    database = Database(tmp_path / "schedule.db")
    database.initialize()
    database.set_setting("schedule_time", "08:00")
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 20, 9, 0, tzinfo=timezone)

    result = next_scheduled_at(database, now)

    assert result == datetime(2026, 7, 21, 8, 0, tzinfo=timezone)

