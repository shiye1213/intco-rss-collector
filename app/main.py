from __future__ import annotations

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from .collector import (
    CollectionAlreadyRunningError,
    CollectionManager,
    Collector,
)
from .content import ArticleContentFetcher
from .database import Database
from .intelligence import (
    ArticleAnalysisManager,
    AutomaticIntelligenceWorkflow,
    DailyReportManager,
    IntelligenceAlreadyRunningError,
    IntelligenceRepository,
)
from .llm import DeepSeekClient, JSONLLMClient
from .prompts import (
    BUSINESS_ANALYSIS_PROMPT_VERSION,
    CATEGORY_LABELS,
    RELEVANCE_PROMPT_VERSION,
    REPORT_PROMPT_VERSION,
)
from .query_builder import build_keyword_query
from .scheduler import DailyScheduler, next_scheduled_at, parse_schedule_time


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "rss_collector.db"
load_dotenv(BASE_DIR / ".env")


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
    country: str = Field(default="", max_length=10)
    active: bool = True

    @field_validator("name", "language")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("url_template")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_http_url(value)

    def validate_mode_template(self) -> None:
        if self.mode == "search" and "{query}" not in self.url_template:
            raise ValueError("搜索型 RSS 地址必须包含 {query} 占位符")


class KeywordQueryPayload(BaseModel):
    match_terms: list[str] = Field(min_length=1, max_length=100)
    context_terms: list[str] = Field(default_factory=list, max_length=100)
    exclude_terms: list[str] = Field(default_factory=list, max_length=100)
    lookback_days: int = Field(default=30, ge=1, le=365)

    @field_validator("match_terms")
    @classmethod
    def normalize_match_terms(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("至少需要一个正文匹配词")
        return cleaned

    @field_validator("context_terms", "exclude_terms")
    @classmethod
    def normalize_optional_terms(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    def build_query(self) -> str:
        return build_keyword_query(
            self.match_terms,
            context_terms=self.context_terms,
            exclude_terms=self.exclude_terms,
            lookback_days=self.lookback_days,
        )


class KeywordPayload(KeywordQueryPayload):
    name: str = Field(min_length=1, max_length=100)
    active: bool = True

    @field_validator("name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


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


class AIAnalysisPayload(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    process_all: bool = True
    force: bool = False
    refresh_content: bool = False
    article_ids: list[int] | None = Field(
        default=None, min_length=1, max_length=100
    )


class AISettingsPayload(BaseModel):
    business_profile: str = Field(min_length=50, max_length=10000)
    relevance_threshold: int = Field(default=70, ge=0, le=100)
    batch_size: int = Field(default=20, ge=1, le=100)
    content_max_chars: int = Field(default=30000, ge=2000, le=100000)
    auto_analyze: bool = False
    auto_report: bool = False

    @field_validator("business_profile")
    @classmethod
    def strip_profile(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 50:
            raise ValueError("企业业务边界至少需要 50 个字符")
        return cleaned


class ReportPayload(BaseModel):
    report_date: date
    categories: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("categories")
    @classmethod
    def normalize_categories(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )


def create_app(
    database_path: Path | None = None,
    llm_client: JSONLLMClient | None = None,
    content_fetcher: ArticleContentFetcher | None = None,
) -> FastAPI:
    database = Database(database_path or DEFAULT_DATABASE_PATH)
    collector = Collector(database)
    manager = CollectionManager(database, collector)
    intelligence_repository = IntelligenceRepository(database)
    intelligence_client = llm_client or DeepSeekClient()
    analysis_manager = ArticleAnalysisManager(
        database,
        intelligence_repository,
        intelligence_client,
        content_fetcher,
    )
    report_manager = DailyReportManager(
        database, intelligence_repository, intelligence_client
    )
    automatic_workflow = AutomaticIntelligenceWorkflow(
        database, analysis_manager, report_manager, intelligence_repository
    )
    manager.on_complete = automatic_workflow.after_collection
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

    app = FastAPI(title="英科医疗 RSS 情报", version="1.2.0", lifespan=lifespan)
    app.state.database = database
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.intelligence_repository = intelligence_repository
    app.state.analysis_manager = analysis_manager
    app.state.report_manager = report_manager

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

    @app.post("/api/keywords/preview")
    def preview_keyword_query(payload: KeywordQueryPayload):
        return {"query": payload.build_query()}

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

    @app.get("/api/ai/status")
    def get_ai_status():
        result = intelligence_repository.status()
        settings = database.get_settings()
        result.update(
            {
                "configured": intelligence_client.configured,
                "model": intelligence_client.model,
                "analysis_running": analysis_manager.running_run_id is not None,
                "analysis_run_id": analysis_manager.running_run_id,
                "report_running": report_manager.running_report_id is not None,
                "report_id": report_manager.running_report_id,
                "categories": CATEGORY_LABELS,
                "relevance_prompt_version": RELEVANCE_PROMPT_VERSION,
                "business_analysis_prompt_version": (
                    BUSINESS_ANALYSIS_PROMPT_VERSION
                ),
                "report_prompt_version": REPORT_PROMPT_VERSION,
                "relevance_threshold": int(
                    settings.get("ai_relevance_threshold", "70")
                ),
                "auto_analyze": settings.get("ai_auto_analyze", "false") == "true",
                "auto_report": settings.get("ai_auto_report", "false") == "true",
            }
        )
        return result

    @app.post("/api/ai/analyze", status_code=status.HTTP_202_ACCEPTED)
    async def start_ai_analysis(payload: AIAnalysisPayload):
        try:
            if payload.process_all:
                run_id, article_ids = analysis_manager.prepare_queue(
                    batch_size=payload.limit,
                    force=payload.force,
                    article_ids=payload.article_ids,
                )
            else:
                run_id, article_ids = analysis_manager.prepare(
                    limit=payload.limit,
                    force=payload.force,
                    article_ids=payload.article_ids,
                )
        except IntelligenceAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if payload.process_all:
            worker = asyncio.to_thread(
                analysis_manager.execute_queue,
                run_id,
                article_ids,
                batch_size=payload.limit,
                trigger_type="manual",
                force=payload.force,
                refresh_content=payload.refresh_content,
            )
        else:
            worker = asyncio.to_thread(
                analysis_manager.execute,
                run_id,
                article_ids,
                force=payload.force,
                refresh_content=payload.refresh_content,
            )
        task = asyncio.create_task(worker)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {
            "run_id": run_id,
            "status": "running",
            "article_count": len(article_ids),
            "batch_size": payload.limit,
            "process_all": payload.process_all,
        }

    @app.get("/api/ai/runs")
    def list_ai_runs(limit: int = Query(default=50, ge=1, le=200)):
        return {"items": intelligence_repository.list_analysis_runs(limit)}

    @app.get("/api/ai/runs/{run_id}")
    def get_ai_run(run_id: int):
        run = intelligence_repository.get_analysis_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="AI 处理日志不存在")
        return run

    @app.get("/api/ai/articles")
    def list_ai_articles(
        category: str = Query(default="", max_length=50),
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        if category and category not in CATEGORY_LABELS:
            raise HTTPException(status_code=422, detail="未知 AI 分类")
        if date_from and date_to and date_to < date_from:
            raise HTTPException(status_code=422, detail="结束日期不能早于开始日期")
        return intelligence_repository.list_business_articles(
            category=category,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/ai/reviews")
    def list_ai_reviews(
        relevant: bool | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        if date_from and date_to and date_to < date_from:
            raise HTTPException(status_code=422, detail="结束日期不能早于开始日期")
        return intelligence_repository.list_reviews(
            relevant=relevant,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/ai/settings")
    def get_ai_settings():
        settings = database.get_settings()
        return {
            "business_profile": settings.get("ai_business_profile", ""),
            "relevance_threshold": int(settings.get("ai_relevance_threshold", "70")),
            "batch_size": int(settings.get("ai_batch_size", "20")),
            "content_max_chars": int(
                settings.get("ai_content_max_chars", "30000")
            ),
            "auto_analyze": settings.get("ai_auto_analyze", "false") == "true",
            "auto_report": settings.get("ai_auto_report", "false") == "true",
        }

    @app.put("/api/ai/settings")
    def update_ai_settings(payload: AISettingsPayload):
        database.set_setting("ai_business_profile", payload.business_profile)
        database.set_setting(
            "ai_relevance_threshold", str(payload.relevance_threshold)
        )
        database.set_setting("ai_batch_size", str(payload.batch_size))
        database.set_setting(
            "ai_content_max_chars", str(payload.content_max_chars)
        )
        database.set_setting("ai_auto_analyze", str(payload.auto_analyze).lower())
        database.set_setting("ai_auto_report", str(payload.auto_report).lower())
        return payload.model_dump()

    @app.post("/api/reports", status_code=status.HTTP_202_ACCEPTED)
    async def create_daily_report(payload: ReportPayload):
        try:
            report_id, articles = report_manager.prepare(
                payload.report_date, payload.categories
            )
        except IntelligenceAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 503 if "DEEPSEEK_API_KEY" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        task = asyncio.create_task(
            asyncio.to_thread(
                report_manager.execute,
                report_id,
                payload.report_date,
                payload.categories,
                articles,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {
            "report_id": report_id,
            "status": "running",
            "article_count": len(articles),
        }

    @app.get("/api/reports")
    def list_daily_reports(limit: int = Query(default=50, ge=1, le=200)):
        return {"items": intelligence_repository.list_reports(limit)}

    @app.get("/api/reports/{report_id}")
    def get_daily_report(report_id: int):
        report = intelligence_repository.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="日报不存在")
        return report

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_handler(_: Request, exc: sqlite3.Error):
        return JSONResponse(
            status_code=500,
            content={"detail": f"数据库错误: {exc}"},
        )

    return app


app = create_app()
