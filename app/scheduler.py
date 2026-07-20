from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .collector import CollectionAlreadyRunningError, CollectionManager
from .database import Database


def parse_schedule_time(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return time(hour=8, minute=0)


def next_scheduled_at(database: Database, now: datetime | None = None) -> datetime:
    settings = database.get_settings()
    timezone = ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
    local_now = now.astimezone(timezone) if now else datetime.now(timezone)
    schedule = parse_schedule_time(settings.get("schedule_time", "08:00"))
    candidate = datetime.combine(local_now.date(), schedule, tzinfo=timezone)
    last_date = settings.get("last_scheduled_date", "")
    if candidate <= local_now or last_date == local_now.date().isoformat():
        candidate += timedelta(days=1)
    return candidate


class DailyScheduler:
    def __init__(self, database: Database, manager: CollectionManager) -> None:
        self.database = database
        self.manager = manager
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                # A scheduler error must not stop later daily checks.
                pass
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=20)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        settings = self.database.get_settings()
        timezone = ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
        local_now = datetime.now(timezone)
        schedule = parse_schedule_time(settings.get("schedule_time", "08:00"))
        today = local_now.date().isoformat()
        if local_now.time() < schedule:
            return
        if settings.get("last_scheduled_date") == today:
            return
        try:
            run_id, started_at = self.manager.prepare("scheduled")
        except CollectionAlreadyRunningError:
            return
        self.database.set_setting("last_scheduled_date", today)
        await asyncio.to_thread(self.manager.execute, run_id, started_at)

