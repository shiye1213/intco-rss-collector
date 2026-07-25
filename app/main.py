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
from .content import ArticleContentReader
from .database import Database
from .intelligence import (
    ArticleAnalysisManager,
    AutomaticIntelligenceWorkflow,
    DailyReportManager,
    IntelligenceAlreadyRunningError,
    IntelligenceRepository,
)
from .llm import DeepSeekClient, JSONLLMClient, OpenAIWebContentReader
from .maintenance import (
    CleanupBusyError,
    CleanupConfirmationError,
    CleanupError,
    CleanupService,
)
from .prompts import (
    BUSINESS_ANALYSIS_PROMPT_VERSION,
    CATEGORY_LABELS,
    DEFAULT_RELEVANCE_PROMPT,
    DEFAULT_REPORT_PROMPT,
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


def normalize_site_domain(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    if candidate.casefold().startswith("site:"):
        candidate = candidate[5:].strip()
    parsed = urlsplit(
        candidate if "://" in candidate else f"https://{candidate}"
    )
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("站点限制必须是有效域名，例如 reuters.com") from exc
    labels = host.split(".")
    valid_labels = all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    )
    if (
        len(labels) < 2
        or len(host) > 253
        or not valid_labels
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("站点限制必须是有效域名，例如 reuters.com")
    return host


class SourcePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url_template: str = Field(min_length=8, max_length=2000)
    mode: Literal["search", "direct"]
    language: str = Field(default="", max_length=30)
    country: str = Field(default="", max_length=10)
    site_domain: str = Field(default="", max_length=253)
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

    @field_validator("site_domain")
    @classmethod
    def validate_site_domain(cls, value: str) -> str:
        return normalize_site_domain(value)

    def validate_mode_template(self) -> None:
        if self.mode == "search" and "{query}" not in self.url_template:
            raise ValueError("搜索型 RSS 地址必须包含 {query} 占位符")
        if self.mode == "direct" and self.site_domain:
            raise ValueError("站点限制只适用于搜索型 RSS")


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
    category_id: int | None = Field(default=None, ge=1)
    active: bool = True

    @field_validator("name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class SettingsPayload(BaseModel):
    schedule_time: str
    incremental_collection: bool = True
    search_local_keyword_filter: bool = True

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
    collection_run_id: int | None = Field(default=None, ge=1)


class AISettingsPayload(BaseModel):
    business_profile: str = Field(min_length=50, max_length=10000)
    relevance_prompt: str = Field(
        default=DEFAULT_RELEVANCE_PROMPT, min_length=20, max_length=20000
    )
    report_prompt: str = Field(
        default=DEFAULT_REPORT_PROMPT, min_length=20, max_length=20000
    )
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

    @field_validator("relevance_prompt", "report_prompt")
    @classmethod
    def strip_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 20:
            raise ValueError("提示词至少需要 20 个字符")
        return cleaned


class ReportPayload(BaseModel):
    report_date: date
    keyword_category_id: int = Field(ge=1)


class DeletePendingPayload(BaseModel):
    confirmation: str = Field(min_length=1, max_length=20)


class CleanupPayload(BaseModel):
    scope: Literal["failed_records", "history", "all_collected"]
    before: date | None = None
    confirmation: str = Field(min_length=1, max_length=20)


def create_app(
    database_path: Path | None = None,
    llm_client: JSONLLMClient | None = None,
    content_reader: ArticleContentReader | None = None,
) -> FastAPI:
    database = Database(database_path or DEFAULT_DATABASE_PATH)
    collector = Collector(database)
    manager = CollectionManager(database, collector)
    intelligence_repository = IntelligenceRepository(database)
    intelligence_client = llm_client or DeepSeekClient()
    intelligence_content_reader = content_reader or OpenAIWebContentReader()
    analysis_manager = ArticleAnalysisManager(
        database,
        intelligence_repository,
        intelligence_client,
        intelligence_content_reader,
    )
    report_manager = DailyReportManager(
        database, intelligence_repository, intelligence_client
    )
    cleanup_service = CleanupService(
        database,
        backup_dir=database.path.parent / "backups",
        is_busy=lambda: any(
            (
                manager.running_run_id is not None,
                analysis_manager.running_run_id is not None,
                report_manager.running_report_id is not None,
            )
        ),
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

    app = FastAPI(title="英科医疗 RSS 情报", version="1.4.0", lifespan=lifespan)
    app.state.database = database
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.intelligence_repository = intelligence_repository
    app.state.analysis_manager = analysis_manager
    app.state.report_manager = report_manager
    app.state.cleanup_service = cleanup_service

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

    @app.get("/api/keyword-categories")
    def list_keyword_categories():
        return {"items": database.get_keyword_categories()}

    @app.get("/api/keyword-hit-stats")
    def keyword_hit_stats():
        return database.keyword_hit_stats()

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
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="关键词组名称已存在") from exc
        return {"id": keyword_id}

    @app.put("/api/keywords/{keyword_id}")
    def update_keyword(keyword_id: int, payload: KeywordPayload):
        try:
            updated = database.update_keyword(keyword_id, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            "incremental_collection": (
                settings.get("incremental_collection", "true") == "true"
            ),
            "search_local_keyword_filter": (
                settings.get("search_local_keyword_filter", "true") == "true"
            ),
        }

    @app.put("/api/settings")
    def update_settings(payload: SettingsPayload):
        database.set_setting("schedule_time", payload.schedule_time)
        database.set_setting(
            "incremental_collection",
            str(payload.incremental_collection).lower(),
        )
        database.set_setting(
            "search_local_keyword_filter",
            str(payload.search_local_keyword_filter).lower(),
        )
        return {
            "schedule_time": payload.schedule_time,
            "timezone": database.get_settings().get("timezone", "Asia/Shanghai"),
            "incremental_collection": payload.incremental_collection,
            "search_local_keyword_filter": payload.search_local_keyword_filter,
        }

    @app.get("/api/ai/status")
    def get_ai_status():
        result = intelligence_repository.status()
        settings = database.get_settings()
        result.update(
            {
                "configured": (
                    intelligence_client.configured
                    and intelligence_content_reader.configured
                ),
                "model": intelligence_client.model,
                "analysis_model": intelligence_client.model,
                "report_configured": intelligence_client.configured,
                "content_reader_configured": (
                    intelligence_content_reader.configured
                ),
                "content_reader_model": getattr(
                    intelligence_content_reader, "model", "custom-content-reader"
                ),
                "analysis_running": analysis_manager.running_run_id is not None,
                "analysis_run_id": analysis_manager.running_run_id,
                "analysis_pause_requested": analysis_manager.pause_requested,
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
        if (
            payload.collection_run_id is not None
            and database.get_run(payload.collection_run_id) is None
        ):
            raise HTTPException(status_code=404, detail="采集任务不存在")
        try:
            if payload.process_all:
                run_id, article_ids = analysis_manager.prepare_queue(
                    batch_size=payload.limit,
                    force=payload.force,
                    article_ids=payload.article_ids,
                    collection_run_id=payload.collection_run_id,
                )
            else:
                run_id, article_ids = analysis_manager.prepare(
                    limit=payload.limit,
                    force=payload.force,
                    article_ids=payload.article_ids,
                    collection_run_id=payload.collection_run_id,
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
            "collection_run_id": payload.collection_run_id,
        }

    @app.post("/api/ai/pause", status_code=status.HTTP_202_ACCEPTED)
    def pause_ai_analysis():
        run_id = analysis_manager.request_pause()
        if run_id is None:
            raise HTTPException(status_code=409, detail="当前没有正在运行的 AI 处理任务")
        return {"run_id": run_id, "status": "pause_requested"}

    @app.post("/api/ai/pending/clear")
    def clear_pending_articles(payload: DeletePendingPayload):
        if payload.confirmation != "DELETE":
            raise HTTPException(status_code=422, detail="请输入 DELETE 确认删除")
        if any(
            (
                manager.running_run_id is not None,
                analysis_manager.running_run_id is not None,
                report_manager.running_report_id is not None,
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="采集、AI 分析或日报任务运行中，不能删除待处理信息",
            )
        return {"deleted": intelligence_repository.delete_pending_articles()}

    @app.get("/api/ai/runs")
    def list_ai_runs(limit: int = Query(default=50, ge=1, le=200)):
        return {"items": intelligence_repository.list_analysis_runs(limit)}

    @app.get("/api/ai/runs/{run_id}")
    def get_ai_run(run_id: int):
        run = intelligence_repository.get_analysis_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="AI 处理日志不存在")
        return run

    @app.get("/api/ai/content-failures")
    def list_content_failures(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return intelligence_repository.list_content_failures(
            limit=limit, offset=offset
        )

    @app.post(
        "/api/ai/content-failures/{article_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_content_failure(article_id: int):
        if analysis_manager.running_run_id is not None:
            raise HTTPException(status_code=409, detail="AI 处理任务正在运行")
        if not intelligence_repository.retry_content_failure(article_id):
            raise HTTPException(status_code=404, detail="全文失败记录不存在")
        try:
            run_id, article_ids = analysis_manager.prepare(
                limit=1, article_ids=[article_id]
            )
        except IntelligenceAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        task = asyncio.create_task(
            asyncio.to_thread(analysis_manager.execute, run_id, article_ids)
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"run_id": run_id, "article_id": article_id, "status": "running"}

    @app.post("/api/ai/content-failures/{article_id}/ignore")
    def ignore_content_failure(article_id: int):
        if analysis_manager.running_run_id is not None:
            raise HTTPException(status_code=409, detail="AI 处理任务正在运行")
        if not intelligence_repository.ignore_content_failure(article_id):
            raise HTTPException(status_code=404, detail="全文失败记录不存在")
        return {"article_id": article_id, "status": "ignored"}

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
            "relevance_prompt": settings.get(
                "ai_relevance_prompt", DEFAULT_RELEVANCE_PROMPT
            ),
            "report_prompt": settings.get(
                "ai_report_prompt", DEFAULT_REPORT_PROMPT
            ),
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
        database.set_setting("ai_relevance_prompt", payload.relevance_prompt)
        database.set_setting("ai_report_prompt", payload.report_prompt)
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

    @app.get("/api/maintenance/cleanup-preview")
    def preview_cleanup(
        scope: Literal["failed_records", "history", "all_collected"],
        before: date | None = None,
    ):
        try:
            return cleanup_service.preview(
                scope, before=before.isoformat() if before else None
            )
        except CleanupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/maintenance/cleanup")
    def execute_cleanup(payload: CleanupPayload):
        try:
            return cleanup_service.execute(
                payload.scope,
                before=payload.before.isoformat() if payload.before else None,
                confirmation=payload.confirmation,
            )
        except CleanupBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (CleanupConfirmationError, CleanupError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/reports", status_code=status.HTTP_202_ACCEPTED)
    async def create_daily_report(payload: ReportPayload):
        try:
            report_id, keyword_category_name, articles = report_manager.prepare(
                payload.report_date, payload.keyword_category_id
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
                keyword_category_name,
                articles,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {
            "report_id": report_id,
            "status": "running",
            "keyword_category_id": payload.keyword_category_id,
            "keyword_category_name": keyword_category_name,
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
