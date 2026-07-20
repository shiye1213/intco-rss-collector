from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .collector import (
    CollectionAlreadyRunningError,
    CollectionManager,
    Collector,
)
from .database import Database
from .scheduler import DailyScheduler, next_scheduled_at, parse_schedule_time


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "rss_collector.db"


def validate_http_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value.replace("{query}", "keyword"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("必须是有效的 HTTP 或 HTTPS 地址")
    return value


class SourcePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url_template: str = Field(min_length=8, max_length=2000)
    mode: Literal["search", "direct"]
    language: str = Field(default="", max_length=30)
    active: bool = True

    @field_validator("name", "language")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("url_template")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_http_url(value)

    def validate_mode_template(self) -> None:
        if self.mode == "search" and "{query}" not in self.url_template:
            raise ValueError("搜索型 RSS 地址必须包含 {query} 占位符")


class KeywordPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=2000)
    match_terms: list[str] = Field(min_length=1, max_length=100)
    active: bool = True

    @field_validator("name", "query")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("match_terms")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("至少需要一个正文匹配词")
        return cleaned


class SettingsPayload(BaseModel):
    schedule_time: str

    @field_validator("schedule_time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError("时间格式必须为 HH:MM")
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("时间必须在 00:00 到 23:59 之间")
        parsed = parse_schedule_time(value)
        return f"{parsed.hour:02d}:{parsed.minute:02d}"


def create_app(database_path: Path | None = None) -> FastAPI:
    database = Database(database_path or DEFAULT_DATABASE_PATH)
    collector = Collector(database)
    manager = CollectionManager(database, collector)
    scheduler = DailyScheduler(database, manager)
    background_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        scheduler.start()
        yield
        await scheduler.stop()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

    app = FastAPI(title="英科医疗 RSS 采集", version="1.0.0", lifespan=lifespan)
    app.state.database = database
    app.state.manager = manager
    app.state.scheduler = scheduler

    @app.middleware("http")
    async def disable_frontend_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def get_status() -> dict[str, object]:
        settings = database.get_settings()
        return {
            "running": manager.running_run_id is not None,
            "running_run_id": manager.running_run_id,
            "article_count": database.article_count(),
            "active_source_count": len(database.get_sources(active_only=True)),
            "active_keyword_count": len(database.get_keywords(active_only=True)),
            "latest_run": database.latest_run(),
            "schedule_time": settings.get("schedule_time", "08:00"),
            "timezone": settings.get("timezone", "Asia/Shanghai"),
            "next_scheduled_at": next_scheduled_at(database).isoformat(),
        }

    @app.post("/api/collections", status_code=status.HTTP_202_ACCEPTED)
    async def start_collection() -> dict[str, object]:
        try:
            run_id, started_at = manager.prepare("manual")
        except CollectionAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        task = asyncio.create_task(asyncio.to_thread(manager.execute, run_id, started_at))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/collections")
    def list_collections(limit: int = Query(default=50, ge=1, le=200)):
        return {"items": database.list_runs(limit)}

    @app.get("/api/collections/{run_id}")
    def get_collection(run_id: int):
        run = database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="采集日志不存在")
        return run

    @app.get("/api/articles")
    def list_articles(
        q: str = Query(default="", max_length=200),
        source_id: int | None = None,
        keyword_id: int | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return database.list_articles(
            query=q.strip(),
            source_id=source_id,
            keyword_id=keyword_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/sources")
    def list_sources():
        return {"items": database.get_sources()}

    @app.post("/api/sources", status_code=status.HTTP_201_CREATED)
    def create_source(payload: SourcePayload):
        try:
            payload.validate_mode_template()
            source_id = database.create_source(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="RSS 源名称或地址已存在") from exc
        return {"id": source_id}

    @app.put("/api/sources/{source_id}")
    def update_source(source_id: int, payload: SourcePayload):
        try:
            payload.validate_mode_template()
            updated = database.update_source(source_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="RSS 源名称已存在") from exc
        if not updated:
            raise HTTPException(status_code=404, detail="RSS 源不存在")
        return {"id": source_id}

    @app.delete("/api/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_source(source_id: int):
        if not database.archive_source(source_id):
            raise HTTPException(status_code=404, detail="RSS 源不存在")

    @app.get("/api/keywords")
    def list_keywords():
        return {"items": database.get_keywords()}

    @app.post("/api/keywords", status_code=status.HTTP_201_CREATED)
    def create_keyword(payload: KeywordPayload):
        try:
            keyword_id = database.create_keyword(payload.model_dump())
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="关键词组名称已存在") from exc
        return {"id": keyword_id}

    @app.put("/api/keywords/{keyword_id}")
    def update_keyword(keyword_id: int, payload: KeywordPayload):
        try:
            updated = database.update_keyword(keyword_id, payload.model_dump())
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="关键词组名称已存在") from exc
        if not updated:
            raise HTTPException(status_code=404, detail="关键词组不存在")
        return {"id": keyword_id}

    @app.delete("/api/keywords/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_keyword(keyword_id: int):
        if not database.archive_keyword(keyword_id):
            raise HTTPException(status_code=404, detail="关键词组不存在")

    @app.get("/api/settings")
    def get_settings():
        settings = database.get_settings()
        return {
            "schedule_time": settings.get("schedule_time", "08:00"),
            "timezone": settings.get("timezone", "Asia/Shanghai"),
        }

    @app.put("/api/settings")
    def update_settings(payload: SettingsPayload):
        database.set_setting("schedule_time", payload.schedule_time)
        return {
            "schedule_time": payload.schedule_time,
            "timezone": database.get_settings().get("timezone", "Asia/Shanghai"),
        }

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_handler(_: Request, exc: sqlite3.Error):
        return JSONResponse(
            status_code=500,
            content={"detail": f"数据库错误: {exc}"},
        )

    return app


app = create_app()
