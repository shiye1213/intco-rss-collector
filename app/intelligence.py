from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .database import Database, utc_now_iso
from .llm import JSONLLMClient, LLMResult
from .prompts import (
    ARTICLE_PROMPT_VERSION,
    CATEGORY_LABELS,
    REPORT_PROMPT_VERSION,
    build_article_prompts,
    build_report_prompts,
)


CategoryCode = Literal[
    "market_demand",
    "competitor",
    "raw_material_supply",
    "policy_regulation",
    "trade_tariff",
    "public_health",
    "technology_product",
    "customer_channel",
    "esg",
    "other",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
ImpactDirection = Literal["positive", "negative", "mixed", "neutral"]


def _clean_string_list(values: list[str], *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values[:limit]:
        cleaned = " ".join(str(value).split())[:500]
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def risk_level_for_score(score: int) -> RiskLevel:
    if score >= 85:
        return "critical"
    if score >= 65:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def local_date_window(
    date_from: date,
    date_to: date | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, str]:
    timezone = ZoneInfo(timezone_name)
    inclusive_end = date_to or date_from
    start = datetime.combine(date_from, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(
        inclusive_end + timedelta(days=1), time.min, tzinfo=timezone
    ).astimezone(UTC)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


class ArticleAssessment(BaseModel):
    is_relevant: bool
    relevance_score: int = Field(ge=0, le=100)
    relevance_reason: str = Field(max_length=1000)
    category: CategoryCode
    secondary_categories: list[CategoryCode] = Field(default_factory=list, max_length=5)
    summary: str = Field(default="", max_length=1500)
    impact_direction: ImpactDirection = "neutral"
    impact_score: int = Field(default=1, ge=1, le=5)
    impact_analysis: str = Field(default="", max_length=2000)
    risk_level: RiskLevel = "low"
    risk_score: int = Field(default=0, ge=0, le=100)
    risk_factors: list[str] = Field(default_factory=list, max_length=8)
    opportunities: list[str] = Field(default_factory=list, max_length=8)
    recommended_actions: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    confidence: int = Field(ge=0, le=100)

    @field_validator(
        "risk_factors", "opportunities", "recommended_actions", "evidence"
    )
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)


class KeyDevelopment(BaseModel):
    article_id: int
    title: str = Field(max_length=500)
    finding: str = Field(max_length=1000)
    business_impact: str = Field(max_length=1000)


class DailyReportAssessment(BaseModel):
    title: str = Field(max_length=300)
    executive_summary: str = Field(max_length=4000)
    risk_level: RiskLevel
    risk_score: int = Field(ge=0, le=100)
    risk_basis: str = Field(max_length=2000)
    key_developments: list[KeyDevelopment] = Field(default_factory=list, max_length=20)
    key_risks: list[str] = Field(default_factory=list, max_length=12)
    opportunities: list[str] = Field(default_factory=list, max_length=12)
    recommended_actions: list[str] = Field(default_factory=list, max_length=12)
    watchlist: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("key_risks", "opportunities", "recommended_actions", "watchlist")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values, limit=12)


class IntelligenceAlreadyRunningError(RuntimeError):
    pass


class IntelligenceRepository:
    JSON_FIELDS = (
        "secondary_categories",
        "risk_factors",
        "opportunities",
        "recommended_actions",
        "evidence",
    )
    REPORT_JSON_FIELDS = (
        "categories",
        "key_developments",
        "key_risks",
        "opportunities",
        "recommended_actions",
        "watchlist",
    )

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _decode_json(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback

    def status(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' AND is_relevant = 1 THEN 1 ELSE 0 END) AS relevant,
                    SUM(CASE WHEN status = 'success' AND is_relevant = 0 THEN 1 ELSE 0 END) AS irrelevant,
                    SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    COUNT(*) AS attempted
                FROM article_analyses
                """
            ).fetchone()
            latest_run = connection.execute(
                "SELECT * FROM ai_analysis_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        relevant = int(row["relevant"] or 0)
        irrelevant = int(row["irrelevant"] or 0)
        processing = int(row["processing"] or 0)
        failed = int(row["failed"] or 0)
        attempted = int(row["attempted"] or 0)
        never_attempted = max(0, total - attempted)
        return {
            "total": total,
            "pending": never_attempted + failed,
            "relevant": relevant,
            "irrelevant": irrelevant,
            "processing": processing,
            "failed": failed,
            "latest_run": dict(latest_run) if latest_run else None,
        }

    def candidate_article_ids(
        self,
        *,
        limit: int,
        force: bool = False,
        article_ids: list[int] | None = None,
    ) -> list[int]:
        filters: list[str] = []
        parameters: list[Any] = []
        if not force:
            filters.append("(aa.article_id IS NULL OR aa.status = 'failed')")
        if article_ids:
            placeholders = ",".join("?" for _ in article_ids)
            filters.append(f"a.id IN ({placeholders})")  # noqa: S608
            parameters.extend(article_ids)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.id
                FROM articles a
                LEFT JOIN article_analyses aa ON aa.article_id = a.id
                {where}
                ORDER BY a.published_at, a.id
                LIMIT ?
                """,  # noqa: S608
                [*parameters, limit],
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def get_article(self, article_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            if row is None:
                return None
            source_rows = connection.execute(
                """
                SELECT s.name, axs.language, axs.country, axs.categories,
                       axs.observed_url
                FROM article_sources axs
                JOIN rss_sources s ON s.id = axs.rss_source_id
                WHERE axs.article_id = ?
                ORDER BY axs.id
                """,
                (article_id,),
            ).fetchall()
            keyword_rows = connection.execute(
                """
                SELECT k.name, ak.matched_terms
                FROM article_keywords ak
                JOIN keywords k ON k.id = ak.keyword_id
                WHERE ak.article_id = ?
                ORDER BY k.name
                """,
                (article_id,),
            ).fetchall()
        article = dict(row)
        article["sources"] = []
        for source_row in source_rows:
            source = dict(source_row)
            source["categories"] = self._decode_json(source["categories"], [])
            article["sources"].append(source)
        article["keywords"] = []
        for keyword_row in keyword_rows:
            keyword = dict(keyword_row)
            keyword["matched_terms"] = self._decode_json(
                keyword["matched_terms"], []
            )
            article["keywords"].append(keyword)
        return article

    def create_analysis_run(
        self,
        article_ids: list[int],
        *,
        trigger_type: str,
        model: str,
    ) -> int:
        now = utc_now_iso()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ai_analysis_runs
                    (trigger_type, status, model, prompt_version, started_at,
                     articles_total)
                VALUES (?, 'running', ?, ?, ?, ?)
                """,
                (trigger_type, model, ARTICLE_PROMPT_VERSION, now, len(article_ids)),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO ai_analysis_run_items (run_id, article_id, status)
                VALUES (?, ?, 'pending')
                """,
                [(run_id, article_id) for article_id in article_ids],
            )
        return run_id

    def mark_processing(self, run_id: int, article_id: int, model: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_analyses
                    (article_id, status, model, prompt_version, error_message)
                VALUES (?, 'processing', ?, ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'processing', model = excluded.model,
                    prompt_version = excluded.prompt_version, error_message = ''
                """,
                (article_id, model, ARTICLE_PROMPT_VERSION),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'processing', error_message = ''
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def save_analysis(
        self,
        run_id: int,
        article_id: int,
        assessment: ArticleAssessment,
        result: LLMResult,
    ) -> None:
        data = assessment.model_dump()
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_analyses
                    (article_id, status, is_relevant, relevance_score,
                     relevance_reason, category, secondary_categories, summary,
                     impact_direction, impact_score, impact_analysis, risk_level,
                     risk_score, risk_factors, opportunities, recommended_actions,
                     evidence, confidence, model, prompt_version, raw_response,
                     prompt_tokens, completion_tokens, analyzed_at, error_message)
                VALUES (?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'success', is_relevant = excluded.is_relevant,
                    relevance_score = excluded.relevance_score,
                    relevance_reason = excluded.relevance_reason,
                    category = excluded.category,
                    secondary_categories = excluded.secondary_categories,
                    summary = excluded.summary,
                    impact_direction = excluded.impact_direction,
                    impact_score = excluded.impact_score,
                    impact_analysis = excluded.impact_analysis,
                    risk_level = excluded.risk_level,
                    risk_score = excluded.risk_score,
                    risk_factors = excluded.risk_factors,
                    opportunities = excluded.opportunities,
                    recommended_actions = excluded.recommended_actions,
                    evidence = excluded.evidence,
                    confidence = excluded.confidence,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    raw_response = excluded.raw_response,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    analyzed_at = excluded.analyzed_at,
                    error_message = ''
                """,
                (
                    article_id,
                    int(assessment.is_relevant),
                    assessment.relevance_score,
                    assessment.relevance_reason,
                    assessment.category,
                    json.dumps(data["secondary_categories"], ensure_ascii=False),
                    assessment.summary,
                    assessment.impact_direction,
                    assessment.impact_score,
                    assessment.impact_analysis,
                    assessment.risk_level,
                    assessment.risk_score,
                    json.dumps(data["risk_factors"], ensure_ascii=False),
                    json.dumps(data["opportunities"], ensure_ascii=False),
                    json.dumps(data["recommended_actions"], ensure_ascii=False),
                    json.dumps(data["evidence"], ensure_ascii=False),
                    assessment.confidence,
                    result.model,
                    ARTICLE_PROMPT_VERSION,
                    result.raw_content[:50000],
                    result.prompt_tokens,
                    result.completion_tokens,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'success', is_relevant = ?, error_message = ''
                WHERE run_id = ? AND article_id = ?
                """,
                (int(assessment.is_relevant), run_id, article_id),
            )

    def fail_analysis(self, run_id: int, article_id: int, message: str) -> None:
        now = utc_now_iso()
        error = message[:2000]
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_analyses
                    (article_id, status, analyzed_at, error_message)
                VALUES (?, 'failed', ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'failed', analyzed_at = excluded.analyzed_at,
                    error_message = excluded.error_message
                """,
                (article_id, now, error),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed', error_message = ?
                WHERE run_id = ? AND article_id = ?
                """,
                (error, run_id, article_id),
            )

    def finish_analysis_run(
        self,
        run_id: int,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        with self.database.connect() as connection:
            counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'success' AND is_relevant = 1 THEN 1 ELSE 0 END) AS relevant,
                    SUM(CASE WHEN status = 'success' AND is_relevant = 0 THEN 1 ELSE 0 END) AS irrelevant,
                    COUNT(*) AS total
                FROM ai_analysis_run_items
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            succeeded = int(counts["succeeded"] or 0)
            failed = int(counts["failed"] or 0)
            total = int(counts["total"] or 0)
            status = "success" if failed == 0 else "failed" if succeeded == 0 else "partial"
            message = "没有待分析文章" if total == 0 else ""
            connection.execute(
                """
                UPDATE ai_analysis_runs
                SET status = ?, finished_at = ?, articles_succeeded = ?,
                    articles_failed = ?, relevant_count = ?, irrelevant_count = ?,
                    prompt_tokens = ?, completion_tokens = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now_iso(),
                    succeeded,
                    failed,
                    int(counts["relevant"] or 0),
                    int(counts["irrelevant"] or 0),
                    prompt_tokens,
                    completion_tokens,
                    message,
                    run_id,
                ),
            )

    def list_analysis_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_analysis_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_analyses(
        self,
        *,
        relevant: bool | None = None,
        category: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = ["aa.status = 'success'"]
        parameters: list[Any] = []
        if relevant is not None:
            filters.append("aa.is_relevant = ?")
            parameters.append(int(relevant))
        if category:
            filters.append("aa.category = ?")
            parameters.append(category)
        if date_from:
            window_start, window_end = local_date_window(
                date_from, date_to or date_from
            )
            filters.extend(["a.published_at >= ?", "a.published_at < ?"])
            parameters.extend([window_start, window_end])
        where = "WHERE " + " AND ".join(filters)
        with self.database.connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM article_analyses aa
                    JOIN articles a ON a.id = aa.article_id
                    {where}
                    """,  # noqa: S608
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT aa.*, a.title, a.url, a.publisher, a.published_at,
                       a.collected_at
                FROM article_analyses aa
                JOIN articles a ON a.id = aa.article_id
                {where}
                ORDER BY a.published_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                [*parameters, limit, offset],
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
        return {"total": total, "items": items}

    def relevant_articles_for_report(
        self, report_date: date, categories: list[str]
    ) -> list[dict[str, Any]]:
        window_start, window_end = local_date_window(report_date)
        filters = [
            "aa.status = 'success'",
            "aa.is_relevant = 1",
            "a.published_at >= ?",
            "a.published_at < ?",
        ]
        parameters: list[Any] = [window_start, window_end]
        if categories:
            placeholders = ",".join("?" for _ in categories)
            filters.append(f"aa.category IN ({placeholders})")  # noqa: S608
            parameters.extend(categories)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT aa.*, a.title, a.url, a.publisher, a.published_at
                FROM article_analyses aa
                JOIN articles a ON a.id = aa.article_id
                WHERE {' AND '.join(filters)}
                ORDER BY aa.risk_score DESC, a.published_at DESC
                """,  # noqa: S608
                parameters,
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
        return items

    def create_report(
        self,
        *,
        report_date: date,
        categories: list[str],
        article_ids: list[int],
        model: str,
    ) -> int:
        now = utc_now_iso()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO daily_reports
                    (report_date, categories, status, article_count, model,
                     prompt_version, created_at, updated_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    report_date.isoformat(),
                    json.dumps(categories, ensure_ascii=False),
                    len(article_ids),
                    model,
                    REPORT_PROMPT_VERSION,
                    now,
                    now,
                ),
            )
            report_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO daily_report_articles (report_id, article_id)
                VALUES (?, ?)
                """,
                [(report_id, article_id) for article_id in article_ids],
            )
        return report_id

    def save_report(
        self,
        report_id: int,
        assessment: DailyReportAssessment,
        result: LLMResult,
    ) -> None:
        data = assessment.model_dump()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE daily_reports
                SET status = 'success', risk_level = ?, risk_score = ?,
                    title = ?, executive_summary = ?, risk_basis = ?,
                    key_developments = ?, key_risks = ?, opportunities = ?,
                    recommended_actions = ?, watchlist = ?, model = ?,
                    raw_response = ?, prompt_tokens = ?, completion_tokens = ?,
                    updated_at = ?, error_message = ''
                WHERE id = ?
                """,
                (
                    assessment.risk_level,
                    assessment.risk_score,
                    assessment.title,
                    assessment.executive_summary,
                    assessment.risk_basis,
                    json.dumps(data["key_developments"], ensure_ascii=False),
                    json.dumps(data["key_risks"], ensure_ascii=False),
                    json.dumps(data["opportunities"], ensure_ascii=False),
                    json.dumps(data["recommended_actions"], ensure_ascii=False),
                    json.dumps(data["watchlist"], ensure_ascii=False),
                    result.model,
                    result.raw_content[:100000],
                    result.prompt_tokens,
                    result.completion_tokens,
                    utc_now_iso(),
                    report_id,
                ),
            )

    def fail_report(self, report_id: int, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE daily_reports
                SET status = 'failed', updated_at = ?, error_message = ?
                WHERE id = ?
                """,
                (utc_now_iso(), message[:2000], report_id),
            )

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM daily_reports ORDER BY report_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        reports = [dict(row) for row in rows]
        for report in reports:
            for field in self.REPORT_JSON_FIELDS:
                report[field] = self._decode_json(report[field], [])
        return reports

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                return None
            article_rows = connection.execute(
                """
                SELECT a.id AS article_id, a.title, a.url, a.publisher,
                       a.published_at,
                       aa.category, aa.summary, aa.impact_analysis,
                       aa.risk_level, aa.risk_score
                FROM daily_report_articles dra
                JOIN articles a ON a.id = dra.article_id
                JOIN article_analyses aa ON aa.article_id = a.id
                WHERE dra.report_id = ?
                ORDER BY aa.risk_score DESC, a.published_at DESC
                """,
                (report_id,),
            ).fetchall()
        report = dict(row)
        for field in self.REPORT_JSON_FIELDS:
            report[field] = self._decode_json(report[field], [])
        report["articles"] = [dict(article) for article in article_rows]
        return report

    def has_successful_report(self, report_date: date) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_reports
                WHERE report_date = ? AND categories = '[]' AND status = 'success'
                LIMIT 1
                """,
                (report_date.isoformat(),),
            ).fetchone()
        return row is not None


class ArticleAnalysisManager:
    def __init__(
        self,
        database: Database,
        repository: IntelligenceRepository,
        client: JSONLLMClient,
    ) -> None:
        self.database = database
        self.repository = repository
        self.client = client
        self._state_lock = threading.Lock()
        self._running_run_id: int | None = None

    @property
    def running_run_id(self) -> int | None:
        with self._state_lock:
            return self._running_run_id

    def prepare(
        self,
        *,
        trigger_type: str = "manual",
        limit: int = 20,
        force: bool = False,
        article_ids: list[int] | None = None,
    ) -> tuple[int, list[int]]:
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        with self._state_lock:
            if self._running_run_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"AI 分析任务 #{self._running_run_id} 正在运行"
                )
            candidate_ids = self.repository.candidate_article_ids(
                limit=limit, force=force, article_ids=article_ids
            )
            run_id = self.repository.create_analysis_run(
                candidate_ids, trigger_type=trigger_type, model=self.client.model
            )
            self._running_run_id = run_id
        return run_id, candidate_ids

    def execute(self, run_id: int, article_ids: list[int]) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        try:
            settings = self.database.get_settings()
            business_profile = settings.get("ai_business_profile", "")
            try:
                threshold = max(
                    0, min(100, int(settings.get("ai_relevance_threshold", "70")))
                )
            except ValueError:
                threshold = 70
            for article_id in article_ids:
                try:
                    article = self.repository.get_article(article_id)
                    if article is None:
                        raise ValueError("文章不存在")
                    self.repository.mark_processing(run_id, article_id, self.client.model)
                    payload = self._article_payload(article)
                    system_prompt, user_prompt = build_article_prompts(
                        payload, business_profile
                    )
                    result = self.client.complete_json(
                        system_prompt, user_prompt, max_tokens=1800
                    )
                    assessment = ArticleAssessment.model_validate(result.data)
                    is_relevant = bool(
                        assessment.is_relevant
                        and assessment.relevance_score >= threshold
                    )
                    if not is_relevant:
                        assessment = assessment.model_copy(
                            update={
                                "is_relevant": False,
                                "category": "other",
                                "secondary_categories": [],
                                "summary": "",
                                "impact_direction": "neutral",
                                "impact_score": 1,
                                "impact_analysis": "",
                                "risk_level": "low",
                                "risk_score": 0,
                                "risk_factors": [],
                                "opportunities": [],
                                "recommended_actions": [],
                            }
                        )
                    else:
                        assessment = assessment.model_copy(
                            update={
                                "is_relevant": True,
                                "risk_level": risk_level_for_score(
                                    assessment.risk_score
                                ),
                            }
                        )
                    self.repository.save_analysis(
                        run_id, article_id, assessment, result
                    )
                    prompt_tokens += result.prompt_tokens
                    completion_tokens += result.completion_tokens
                except Exception as exc:
                    self.repository.fail_analysis(
                        run_id, article_id, f"{type(exc).__name__}: {exc}"
                    )
        finally:
            self.repository.finish_analysis_run(
                run_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            with self._state_lock:
                if self._running_run_id == run_id:
                    self._running_run_id = None

    @staticmethod
    def _article_payload(article: dict[str, Any]) -> dict[str, Any]:
        return {
            "article_id": article["id"],
            "title": article["title"],
            "publisher": article["publisher_normalized"] or article["publisher"],
            "published_at": article["published_at"],
            "summary": article["summary"][:8000],
            "sources": [
                {
                    "name": source["name"],
                    "language": source["language"],
                    "country": source["country"],
                    "categories": source["categories"],
                }
                for source in article["sources"]
            ],
            "keyword_groups": article["keywords"],
        }


class DailyReportManager:
    def __init__(
        self,
        database: Database,
        repository: IntelligenceRepository,
        client: JSONLLMClient,
    ) -> None:
        self.database = database
        self.repository = repository
        self.client = client
        self._state_lock = threading.Lock()
        self._running_report_id: int | None = None

    @property
    def running_report_id(self) -> int | None:
        with self._state_lock:
            return self._running_report_id

    def prepare(
        self, report_date: date, categories: list[str]
    ) -> tuple[int, list[dict[str, Any]]]:
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        invalid_categories = [
            category for category in categories if category not in CATEGORY_LABELS
        ]
        if invalid_categories:
            raise ValueError(f"未知分类: {', '.join(invalid_categories)}")
        with self._state_lock:
            if self._running_report_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"日报 #{self._running_report_id} 正在生成"
                )
            articles = self.repository.relevant_articles_for_report(
                report_date, categories
            )
            if not articles:
                raise ValueError("所选日期和分类下没有已通过相关性审核的新闻")
            report_id = self.repository.create_report(
                report_date=report_date,
                categories=categories,
                article_ids=[int(article["article_id"]) for article in articles],
                model=self.client.model,
            )
            self._running_report_id = report_id
        return report_id, articles

    def execute(
        self,
        report_id: int,
        report_date: date,
        categories: list[str],
        articles: list[dict[str, Any]],
    ) -> None:
        try:
            settings = self.database.get_settings()
            report_articles = [self._report_article(article) for article in articles]
            system_prompt, user_prompt = build_report_prompts(
                report_date=report_date.isoformat(),
                category_labels=[CATEGORY_LABELS[category] for category in categories],
                articles=report_articles,
                business_profile=settings.get("ai_business_profile", ""),
            )
            result = self.client.complete_json(
                system_prompt, user_prompt, max_tokens=3000
            )
            assessment = DailyReportAssessment.model_validate(result.data)
            valid_article_ids = {int(article["article_id"]) for article in articles}
            developments = [
                development
                for development in assessment.key_developments
                if development.article_id in valid_article_ids
            ]
            article_floor = max(int(article["risk_score"]) for article in articles)
            risk_score = max(assessment.risk_score, article_floor)
            assessment = assessment.model_copy(
                update={
                    "key_developments": developments,
                    "risk_score": risk_score,
                    "risk_level": risk_level_for_score(risk_score),
                }
            )
            self.repository.save_report(report_id, assessment, result)
        except Exception as exc:
            self.repository.fail_report(report_id, f"{type(exc).__name__}: {exc}")
        finally:
            with self._state_lock:
                if self._running_report_id == report_id:
                    self._running_report_id = None

    @staticmethod
    def _report_article(article: dict[str, Any]) -> dict[str, Any]:
        return {
            "article_id": article["article_id"],
            "title": article["title"],
            "publisher": article["publisher"],
            "published_at": article["published_at"],
            "category": article["category"],
            "summary": article["summary"],
            "impact_direction": article["impact_direction"],
            "impact_score": article["impact_score"],
            "impact_analysis": article["impact_analysis"],
            "risk_level": article["risk_level"],
            "risk_score": article["risk_score"],
            "risk_factors": article["risk_factors"],
            "opportunities": article["opportunities"],
            "recommended_actions": article["recommended_actions"],
            "evidence": article["evidence"],
        }


class AutomaticIntelligenceWorkflow:
    def __init__(
        self,
        database: Database,
        analysis_manager: ArticleAnalysisManager,
        report_manager: DailyReportManager,
        repository: IntelligenceRepository,
    ) -> None:
        self.database = database
        self.analysis_manager = analysis_manager
        self.report_manager = report_manager
        self.repository = repository

    def after_collection(self) -> None:
        settings = self.database.get_settings()
        if settings.get("ai_auto_analyze", "false").lower() != "true":
            return
        if not self.analysis_manager.client.configured:
            return
        try:
            limit = max(1, min(100, int(settings.get("ai_batch_size", "20"))))
        except ValueError:
            limit = 20
        try:
            run_id, article_ids = self.analysis_manager.prepare(
                trigger_type="collection", limit=limit
            )
            self.analysis_manager.execute(run_id, article_ids)
        except IntelligenceAlreadyRunningError:
            return
        if settings.get("ai_auto_report", "false").lower() != "true":
            return
        timezone = ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
        report_date = datetime.now(timezone).date()
        if self.repository.has_successful_report(report_date):
            return
        try:
            report_id, articles = self.report_manager.prepare(report_date, [])
            self.report_manager.execute(report_id, report_date, [], articles)
        except (IntelligenceAlreadyRunningError, ValueError):
            return
