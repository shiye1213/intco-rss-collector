from __future__ import annotations

import json
import re
import threading
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from .content import (
    ArticleContentReader,
    ArticleReference,
    ContentDocument,
    ContentFetchError,
)
from .database import Database, utc_now_iso
from .llm import JSONLLMClient, LLMResult
from .prompts import (
    BUSINESS_ANALYSIS_PROMPT_VERSION,
    CATEGORY_LABELS,
    DEFAULT_REPORT_CATEGORY_PROMPTS,
    DEFAULT_RELEVANCE_PROMPT,
    DEFAULT_REPORT_PROMPT,
    KEYWORD_CATEGORY_BUSINESS_CODES,
    RELEVANCE_PROMPT_VERSION,
    REPORT_CATEGORY_SETTING_KEYS,
    REPORT_PROMPT_VERSION,
    build_business_analysis_prompts,
    build_relevance_prompts,
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
KeywordCategoryName = Literal["贸易政策", "关税调整", "行业法规"]

_COMPANY_NAME_PATTERN = re.compile(r"英科医疗|\bIntco\b", re.IGNORECASE)


def _remove_unattributed_company_sentences(value: str) -> str:
    sentences = re.split(r"(?<=[。！？!?])|\n+", value)
    return "".join(
        sentence
        for sentence in sentences
        if sentence.strip() and not _COMPANY_NAME_PATTERN.search(sentence)
    ).strip()
RiskLevel = Literal["low", "medium", "high", "critical"]
ImpactDirection = Literal["positive", "negative", "mixed", "neutral"]

CONTENT_FETCH_MAX_ATTEMPTS = 3
CONTENT_RETRY_BASE_MINUTES = 5
CONTENT_RETRY_MAX_MINUTES = 24 * 60


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


class RelevanceAssessment(BaseModel):
    is_relevant: bool
    relevance_score: int = Field(ge=0, le=100)
    relevance_reason: str = Field(min_length=1, max_length=1000)
    category: CategoryCode
    secondary_categories: list[CategoryCode] = Field(
        default_factory=list, max_length=2
    )
    keyword_categories: list[KeywordCategoryName] = Field(
        default_factory=list, max_length=3
    )
    evidence: list[str] = Field(default_factory=list, max_length=8)
    confidence: int = Field(ge=0, le=100)

    @field_validator("secondary_categories")
    @classmethod
    def normalize_categories(
        cls, values: list[CategoryCode]
    ) -> list[CategoryCode]:
        return list(dict.fromkeys(values))

    @field_validator("keyword_categories")
    @classmethod
    def normalize_keyword_categories(
        cls, values: list[KeywordCategoryName]
    ) -> list[KeywordCategoryName]:
        return list(dict.fromkeys(values))

    @field_validator("evidence")
    @classmethod
    def normalize_evidence(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)

    @model_validator(mode="after")
    def normalize_category_relationships(self) -> RelevanceAssessment:
        if not self.is_relevant:
            self.category = "other"
            self.secondary_categories = []
            self.keyword_categories = []
            return self
        self.secondary_categories = [
            value for value in self.secondary_categories if value != self.category
        ]
        return self


class BusinessAnalysis(BaseModel):
    category: CategoryCode
    secondary_categories: list[CategoryCode] = Field(default_factory=list, max_length=2)
    summary: str = Field(min_length=1, max_length=1500)
    impact_direction: ImpactDirection = "neutral"
    impact_score: int = Field(default=1, ge=1, le=5)
    impact_analysis: str = Field(min_length=1, max_length=2000)
    risk_level: RiskLevel = "low"
    risk_score: int = Field(default=0, ge=0, le=100)
    risk_factors: list[str] = Field(default_factory=list, max_length=8)
    opportunities: list[str] = Field(default_factory=list, max_length=8)
    recommended_actions: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @field_validator(
        "risk_factors", "opportunities", "recommended_actions", "evidence"
    )
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return _clean_string_list(values)

    @model_validator(mode="after")
    def remove_primary_from_secondary(self) -> BusinessAnalysis:
        self.secondary_categories = [
            value for value in self.secondary_categories if value != self.category
        ]
        return self


def enforce_company_fact_boundary(
    *,
    full_text: str,
    review: RelevanceAssessment | None = None,
    analysis: BusinessAnalysis | None = None,
) -> RelevanceAssessment | BusinessAnalysis:
    """Remove company-specific claims when the model input never names the company."""
    if _COMPANY_NAME_PATTERN.search(full_text):
        if review is not None:
            return review
        if analysis is not None:
            return analysis
        raise ValueError("review 或 analysis 至少提供一个")

    if review is not None:
        reason = _remove_unattributed_company_sentences(review.relevance_reason)
        boundary_note = (
            "正文未明确提及英科医疗；相关性仅基于正文所述行业事件与企业"
            "业务边界的潜在传导，企业实际暴露需进一步核实。"
        )
        reason = f"{reason}{boundary_note}" if reason else boundary_note
        return review.model_copy(
            update={
                "relevance_reason": reason[:1000],
                "evidence": [
                    item
                    for item in review.evidence
                    if not _COMPANY_NAME_PATTERN.search(item)
                ],
            }
        )

    if analysis is not None:
        summary = _remove_unattributed_company_sentences(analysis.summary)
        impact = _remove_unattributed_company_sentences(analysis.impact_analysis)
        category_path = {
            "market_demand": "需求、采购或价格变化可能传导至同类产品的订单与售价。",
            "competitor": "竞争者的产能、定价或经营变化可能改变行业竞争强度。",
            "raw_material_supply": "原材料或供应变化可能传导至同类制造企业的成本与交付。",
            "policy_regulation": "政策或准入规则可能改变同类产品的合规要求与订单机会。",
            "trade_tariff": "税费变化可能改变同类产品的到岸成本与市场准入。",
        }.get(
            str(analysis.category),
            "该行业事件可能通过企业业务边界所列路径产生传导。",
        )
        boundary_note = (
            "正文未明确提及英科医疗，以上仅为同类制造企业的行业传导；"
            "企业实际市场、产地、客户及供应链暴露需进一步核实。"
        )

        def clean_list(values: list[str]) -> list[str]:
            return [
                cleaned
                for value in values
                if (cleaned := _remove_unattributed_company_sentences(value))
            ]

        return analysis.model_copy(
            update={
                "summary": summary
                or "正文描述了与企业业务边界相关的行业事件。",
                "impact_analysis": f"{impact}{boundary_note}"
                if impact
                else f"{category_path}{boundary_note}",
                "risk_factors": clean_list(analysis.risk_factors),
                "opportunities": clean_list(analysis.opportunities),
                "recommended_actions": clean_list(analysis.recommended_actions),
                "evidence": [
                    item
                    for item in analysis.evidence
                    if not _COMPANY_NAME_PATTERN.search(item)
                ],
            }
        )

    raise ValueError("review 或 analysis 至少提供一个")


class KeyDevelopment(BaseModel):
    article_id: int
    category: CategoryCode
    title: str = Field(max_length=500)
    finding: str = Field(max_length=1000)
    business_impact: str = Field(max_length=1000)


class CitedReportItem(BaseModel):
    category: CategoryCode
    content: str = Field(min_length=1, max_length=1000)
    article_ids: list[int] = Field(default_factory=list, max_length=8)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return " ".join(value.split())[:1000]

    @field_validator("article_ids")
    @classmethod
    def normalize_article_ids(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(value for value in values if value > 0))


class DailyReportAssessment(BaseModel):
    title: str = Field(max_length=300)
    executive_summary: str = Field(max_length=4000)
    risk_level: RiskLevel
    risk_score: int = Field(ge=0, le=100)
    risk_basis: str = Field(max_length=2000)
    key_developments: list[KeyDevelopment] = Field(default_factory=list, max_length=20)
    key_risks: list[CitedReportItem] = Field(default_factory=list, max_length=12)
    opportunities: list[CitedReportItem] = Field(default_factory=list, max_length=12)
    recommended_actions: list[CitedReportItem] = Field(
        default_factory=list, max_length=12
    )
    watchlist: list[CitedReportItem] = Field(default_factory=list, max_length=12)

    @field_validator(
        "key_risks",
        "opportunities",
        "recommended_actions",
        "watchlist",
        mode="before",
    )
    @classmethod
    def normalize_cited_items(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        return [
            {
                "category": "other",
                "content": value,
                "article_ids": [],
            }
            if isinstance(value, str)
            else value
            for value in values[:12]
        ]


class IntelligenceAlreadyRunningError(RuntimeError):
    pass


class _ProviderRateLimited(RuntimeError):
    """Stop a queue when a shared upstream provider rejects the batch."""


class IntelligenceRepository:
    REVIEW_JSON_FIELDS = (
        "secondary_categories",
        "keyword_categories",
        "evidence",
    )
    BUSINESS_JSON_FIELDS = (
        "relevance_evidence",
        "secondary_categories",
        "risk_factors",
        "opportunities",
        "recommended_actions",
        "analysis_evidence",
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

    @staticmethod
    def _candidate_condition() -> str:
        return """
            ic.article_id IS NULL
            OR (
                ic.status = 'failed'
                AND ic.is_terminal = 0
                AND ic.ignored_at IS NULL
                AND (
                    ic.next_retry_at IS NULL
                    OR datetime(ic.next_retry_at) <= datetime('now')
                )
            )
            OR (
                ic.status = 'success'
                AND (
                    rr.article_id IS NULL OR rr.status = 'failed'
                    OR rr.content_hash <> ic.content_hash
                    OR (
                        rr.status = 'success' AND rr.is_relevant = 1
                        AND (
                            ba.article_id IS NULL OR ba.analysis_status = 'failed'
                            OR ba.content_hash <> ic.content_hash
                        )
                    )
                )
            )
        """

    def status(self) -> dict[str, Any]:
        condition = self._candidate_condition()
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN ic.status = 'success' THEN 1 ELSE 0 END) AS content_ready,
                    SUM(CASE WHEN ic.status = 'failed' THEN 1 ELSE 0 END) AS content_failed,
                    SUM(CASE WHEN ic.status = 'failed'
                        AND ic.is_terminal = 0 AND ic.ignored_at IS NULL
                        AND ic.next_retry_at IS NOT NULL
                        AND datetime(ic.next_retry_at) > datetime('now')
                        THEN 1 ELSE 0 END) AS content_retry_waiting,
                    SUM(CASE WHEN ic.status = 'failed'
                        AND ic.is_terminal = 1 AND ic.ignored_at IS NULL
                        THEN 1 ELSE 0 END) AS content_final_failed,
                    SUM(CASE WHEN ic.status = 'failed'
                        AND ic.ignored_at IS NOT NULL
                        THEN 1 ELSE 0 END) AS content_ignored,
                    SUM(CASE WHEN ic.status = 'success'
                        AND rr.status = 'success'
                        AND rr.content_hash = ic.content_hash
                        AND rr.is_relevant = 1 THEN 1 ELSE 0 END) AS relevant,
                    SUM(CASE WHEN ic.status = 'success'
                        AND rr.status = 'success'
                        AND rr.content_hash = ic.content_hash
                        AND rr.is_relevant = 0 THEN 1 ELSE 0 END) AS irrelevant,
                    SUM(CASE WHEN ic.status = 'success'
                        AND rr.status = 'failed'
                        AND rr.content_hash = ic.content_hash
                        THEN 1 ELSE 0 END) AS review_failed,
                    SUM(CASE WHEN ic.status = 'success'
                        AND ba.analysis_status = 'success'
                        AND ba.content_hash = ic.content_hash
                        THEN 1 ELSE 0 END) AS analyzed,
                    SUM(CASE WHEN ic.status = 'success'
                        AND ba.analysis_status = 'failed'
                        AND ba.content_hash = ic.content_hash
                        THEN 1 ELSE 0 END) AS analysis_failed,
                    SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS pending
                FROM articles a
                LEFT JOIN article_contents ic ON ic.article_id = a.id
                LEFT JOIN article_relevance_reviews rr ON rr.article_id = a.id
                LEFT JOIN business_articles ba ON ba.article_id = a.id
                """  # noqa: S608
            ).fetchone()
            latest_run = connection.execute(
                "SELECT * FROM ai_analysis_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "pending": int(row["pending"] or 0),
            "content_ready": int(row["content_ready"] or 0),
            "content_failed": int(row["content_failed"] or 0),
            "content_retry_waiting": int(row["content_retry_waiting"] or 0),
            "content_final_failed": int(row["content_final_failed"] or 0),
            "content_ignored": int(row["content_ignored"] or 0),
            "relevant": int(row["relevant"] or 0),
            "irrelevant": int(row["irrelevant"] or 0),
            "review_failed": int(row["review_failed"] or 0),
            "analyzed": int(row["analyzed"] or 0),
            "analysis_failed": int(row["analysis_failed"] or 0),
            "failed": int(row["content_failed"] or 0)
            + int(row["review_failed"] or 0)
            + int(row["analysis_failed"] or 0),
            "latest_run": dict(latest_run) if latest_run else None,
        }

    def delete_pending_articles(self) -> int:
        condition = self._candidate_condition()
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM articles
                WHERE id IN (
                    SELECT a.id
                    FROM articles a
                    LEFT JOIN article_contents ic ON ic.article_id = a.id
                    LEFT JOIN article_relevance_reviews rr ON rr.article_id = a.id
                    LEFT JOIN business_articles ba ON ba.article_id = a.id
                    WHERE ({condition})
                )
                """  # noqa: S608
            )
            return int(cursor.rowcount)

    def candidate_article_ids(
        self,
        *,
        limit: int | None,
        force: bool = False,
        article_ids: list[int] | None = None,
        collection_run_id: int | None = None,
    ) -> list[int]:
        filters: list[str] = []
        parameters: list[Any] = []
        if not force:
            filters.append(f"({self._candidate_condition()})")
        if article_ids:
            placeholders = ",".join("?" for _ in article_ids)
            filters.append(f"a.id IN ({placeholders})")  # noqa: S608
            parameters.extend(article_ids)
        if collection_run_id is not None:
            filters.append(
                "a.collected_at = (SELECT started_at FROM collection_runs WHERE id = ?)"
            )
            parameters.append(collection_run_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        limit_clause = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.id
                FROM articles a
                LEFT JOIN article_contents ic ON ic.article_id = a.id
                LEFT JOIN article_relevance_reviews rr ON rr.article_id = a.id
                LEFT JOIN business_articles ba ON ba.article_id = a.id
                {where}
                ORDER BY
                    CASE
                        WHEN ic.article_id IS NULL THEN 0
                        WHEN ic.status = 'success' THEN 1
                        ELSE 2
                    END,
                    a.published_at DESC, a.id DESC
                {limit_clause}
                """,  # noqa: S608
                parameters,
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
                       axs.observed_url, axs.canonical_url
                FROM article_sources axs
                JOIN rss_sources s ON s.id = axs.rss_source_id
                WHERE axs.article_id = ?
                ORDER BY axs.id
                """,
                (article_id,),
            ).fetchall()
            keyword_rows = connection.execute(
                """
                SELECT k.name, kc.name AS category_name, ak.matched_terms
                FROM article_keywords ak
                JOIN keywords k ON k.id = ak.keyword_id
                LEFT JOIN keyword_categories kc ON kc.id = k.category_id
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

    def get_content(self, article_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_contents WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_review(self, article_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM article_relevance_reviews WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        if row is None:
            return None
        review = dict(row)
        for field in self.REVIEW_JSON_FIELDS:
            review[field] = self._decode_json(review[field], [])
        return review

    def create_analysis_run(
        self,
        article_ids: list[int],
        *,
        trigger_type: str,
        model: str,
    ) -> int:
        now = utc_now_iso()
        prompt_version = (
            f"{RELEVANCE_PROMPT_VERSION}|{BUSINESS_ANALYSIS_PROMPT_VERSION}"
        )
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ai_analysis_runs
                    (trigger_type, status, model, prompt_version, started_at,
                     articles_total)
                VALUES (?, 'running', ?, ?, ?, ?)
                """,
                (trigger_type, model, prompt_version, now, len(article_ids)),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO ai_analysis_run_items
                    (run_id, article_id, status, content_status,
                     relevance_status, business_analysis_status)
                VALUES (?, ?, 'pending', 'pending', 'pending', 'pending')
                """,
                [(run_id, article_id) for article_id in article_ids],
            )
        return run_id

    def mark_content_processing(
        self, run_id: int, article_id: int, requested_url: str
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_contents
                    (article_id, status, requested_url, error_message)
                VALUES (?, 'processing', ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'processing', requested_url = excluded.requested_url,
                    next_retry_at = NULL, error_message = ''
                """,
                (article_id, requested_url),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'processing', content_status = 'processing',
                    error_message = ''
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def save_content(
        self, run_id: int, article_id: int, document: ContentDocument
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_contents
                    (article_id, status, requested_url, final_url, full_text,
                     content_hash, content_chars, http_status, content_type,
                     extractor, fetched_at, error_message)
                VALUES (?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'success', requested_url = excluded.requested_url,
                    final_url = excluded.final_url, full_text = excluded.full_text,
                    content_hash = excluded.content_hash,
                    content_chars = excluded.content_chars,
                    http_status = excluded.http_status,
                    content_type = excluded.content_type,
                    extractor = excluded.extractor,
                    fetched_at = excluded.fetched_at, error_message = '',
                    attempt_count = 0, failure_kind = '', next_retry_at = NULL,
                    is_terminal = 0, ignored_at = NULL
                """,
                (
                    article_id,
                    document.requested_url,
                    document.final_url,
                    document.full_text,
                    document.content_hash,
                    document.content_chars,
                    document.http_status,
                    document.content_type,
                    document.extractor,
                    utc_now_iso(),
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET content_status = 'success'
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def reuse_content(self, run_id: int, article_id: int) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'processing', content_status = 'success'
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def fail_content(
        self,
        run_id: int,
        article_id: int,
        requested_url: str,
        message: str,
        *,
        failure_kind: str,
        retryable: bool,
    ) -> None:
        now_value = datetime.now(UTC)
        now = now_value.isoformat().replace("+00:00", "Z")
        error = message[:2000]
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT attempt_count FROM article_contents WHERE article_id = ?",
                (article_id,),
            ).fetchone()
            attempt_count = int(existing["attempt_count"] if existing else 0) + 1
            is_terminal = (not retryable) or (
                attempt_count >= CONTENT_FETCH_MAX_ATTEMPTS
            )
            next_retry_at: str | None = None
            if retryable and not is_terminal:
                delay_minutes = min(
                    CONTENT_RETRY_MAX_MINUTES,
                    CONTENT_RETRY_BASE_MINUTES * (2 ** (attempt_count - 1)),
                )
                next_retry_at = (
                    now_value + timedelta(minutes=delay_minutes)
                ).isoformat().replace("+00:00", "Z")
            connection.execute(
                """
                INSERT INTO article_contents
                    (article_id, status, requested_url, fetched_at, error_message,
                     attempt_count, failure_kind, next_retry_at, is_terminal,
                     ignored_at)
                VALUES (?, 'failed', ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'failed', requested_url = excluded.requested_url,
                    fetched_at = excluded.fetched_at,
                    error_message = excluded.error_message,
                    attempt_count = excluded.attempt_count,
                    failure_kind = excluded.failure_kind,
                    next_retry_at = excluded.next_retry_at,
                    is_terminal = excluded.is_terminal,
                    ignored_at = NULL
                """,
                (
                    article_id,
                    requested_url,
                    now,
                    error,
                    attempt_count,
                    failure_kind[:100],
                    next_retry_at,
                    int(is_terminal),
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed', content_status = 'failed',
                    relevance_status = 'skipped',
                    business_analysis_status = 'skipped', error_message = ?
                WHERE run_id = ? AND article_id = ?
                """,
                (error, run_id, article_id),
            )

    def list_content_failures(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM article_contents WHERE status = 'failed'"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT ic.*, a.title, a.url, a.publisher, a.published_at,
                       CASE
                           WHEN ic.ignored_at IS NOT NULL THEN 'ignored'
                           WHEN ic.is_terminal = 1 THEN 'final_failed'
                           WHEN ic.next_retry_at IS NOT NULL
                                AND datetime(ic.next_retry_at) > datetime('now')
                               THEN 'waiting'
                           ELSE 'retry_ready'
                       END AS disposition
                FROM article_contents ic
                JOIN articles a ON a.id = ic.article_id
                WHERE ic.status = 'failed'
                ORDER BY
                    CASE
                        WHEN ic.ignored_at IS NULL AND ic.is_terminal = 1 THEN 0
                        WHEN ic.ignored_at IS NULL THEN 1
                        ELSE 2
                    END,
                    ic.fetched_at DESC, ic.article_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total}

    def retry_content_failure(self, article_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE article_contents
                SET attempt_count = 0, failure_kind = '', next_retry_at = NULL,
                    is_terminal = 0, ignored_at = NULL
                WHERE article_id = ? AND status = 'failed'
                """,
                (article_id,),
            )
            return cursor.rowcount > 0

    def ignore_content_failure(self, article_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE article_contents
                SET is_terminal = 1, next_retry_at = NULL, ignored_at = ?
                WHERE article_id = ? AND status = 'failed'
                """,
                (utc_now_iso(), article_id),
            )
            return cursor.rowcount > 0

    def mark_relevance_processing(
        self,
        run_id: int,
        article_id: int,
        *,
        model: str,
        content_hash: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_relevance_reviews
                    (article_id, status, content_hash, model, prompt_version,
                     error_message)
                VALUES (?, 'processing', ?, ?, ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'processing', content_hash = excluded.content_hash,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    error_message = ''
                """,
                (
                    article_id,
                    content_hash,
                    model,
                    RELEVANCE_PROMPT_VERSION,
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET relevance_status = 'processing'
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def save_relevance(
        self,
        run_id: int,
        article_id: int,
        assessment: RelevanceAssessment,
        result: LLMResult,
        *,
        content_hash: str,
    ) -> None:
        now = utc_now_iso()
        evidence = json.dumps(assessment.evidence, ensure_ascii=False)
        secondary_categories = json.dumps(
            assessment.secondary_categories, ensure_ascii=False
        )
        keyword_categories = json.dumps(
            assessment.keyword_categories, ensure_ascii=False
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_relevance_reviews
                    (article_id, status, is_relevant, relevance_score,
                     relevance_reason, category, secondary_categories,
                     keyword_categories, evidence, confidence, content_hash,
                     model, prompt_version, raw_response, prompt_tokens,
                     completion_tokens, reviewed_at, error_message)
                VALUES (
                    ?, 'success', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ''
                )
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'success', is_relevant = excluded.is_relevant,
                    relevance_score = excluded.relevance_score,
                    relevance_reason = excluded.relevance_reason,
                    category = excluded.category,
                    secondary_categories = excluded.secondary_categories,
                    keyword_categories = excluded.keyword_categories,
                    evidence = excluded.evidence,
                    confidence = excluded.confidence,
                    content_hash = excluded.content_hash,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    raw_response = excluded.raw_response,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    reviewed_at = excluded.reviewed_at, error_message = ''
                """,
                (
                    article_id,
                    int(assessment.is_relevant),
                    assessment.relevance_score,
                    assessment.relevance_reason,
                    assessment.category,
                    secondary_categories,
                    keyword_categories,
                    evidence,
                    assessment.confidence,
                    content_hash,
                    result.model,
                    RELEVANCE_PROMPT_VERSION,
                    result.raw_content[:50000],
                    result.prompt_tokens,
                    result.completion_tokens,
                    now,
                ),
            )
            if assessment.is_relevant:
                connection.execute(
                    """
                    UPDATE ai_analysis_run_items
                    SET is_relevant = 1, relevance_status = 'success'
                    WHERE run_id = ? AND article_id = ?
                    """,
                    (run_id, article_id),
                )
            else:
                connection.execute(
                    "DELETE FROM business_articles WHERE article_id = ?",
                    (article_id,),
                )
                connection.execute(
                    """
                    UPDATE ai_analysis_run_items
                    SET status = 'success', is_relevant = 0,
                        relevance_status = 'success',
                        business_analysis_status = 'skipped', error_message = ''
                    WHERE run_id = ? AND article_id = ?
                    """,
                    (run_id, article_id),
                )

    def reuse_relevance(
        self, run_id: int, article_id: int, *, is_relevant: bool
    ) -> None:
        with self.database.connect() as connection:
            if is_relevant:
                connection.execute(
                    """
                    UPDATE ai_analysis_run_items
                    SET is_relevant = 1, relevance_status = 'success'
                    WHERE run_id = ? AND article_id = ?
                    """,
                    (run_id, article_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE ai_analysis_run_items
                    SET status = 'success', is_relevant = 0,
                        relevance_status = 'success',
                        business_analysis_status = 'skipped'
                    WHERE run_id = ? AND article_id = ?
                    """,
                    (run_id, article_id),
                )

    def fail_relevance(
        self,
        run_id: int,
        article_id: int,
        message: str,
        *,
        content_hash: str,
    ) -> None:
        now = utc_now_iso()
        error = message[:2000]
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_relevance_reviews
                    (article_id, status, content_hash, reviewed_at, error_message)
                VALUES (?, 'failed', ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                    status = 'failed', content_hash = excluded.content_hash,
                    reviewed_at = excluded.reviewed_at,
                    error_message = excluded.error_message
                """,
                (article_id, content_hash, now, error),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed', relevance_status = 'failed',
                    business_analysis_status = 'skipped', error_message = ?
                WHERE run_id = ? AND article_id = ?
                """,
                (error, run_id, article_id),
            )

    def mark_business_processing(
        self,
        run_id: int,
        article_id: int,
        review: RelevanceAssessment,
        *,
        content_hash: str,
        model: str,
    ) -> None:
        now = utc_now_iso()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO business_articles
                    (article_id, analysis_status, relevance_score,
                     relevance_reason, relevance_confidence,
                     relevance_evidence, content_hash, model, prompt_version,
                     accepted_at, error_message)
                VALUES (?, 'processing', ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(article_id) DO UPDATE SET
                    analysis_status = 'processing',
                    relevance_score = excluded.relevance_score,
                    relevance_reason = excluded.relevance_reason,
                    relevance_confidence = excluded.relevance_confidence,
                    relevance_evidence = excluded.relevance_evidence,
                    content_hash = excluded.content_hash,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    accepted_at = excluded.accepted_at, error_message = ''
                """,
                (
                    article_id,
                    review.relevance_score,
                    review.relevance_reason,
                    review.confidence,
                    json.dumps(review.evidence, ensure_ascii=False),
                    content_hash,
                    model,
                    BUSINESS_ANALYSIS_PROMPT_VERSION,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET business_analysis_status = 'processing'
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def save_business_analysis(
        self,
        run_id: int,
        article_id: int,
        analysis: BusinessAnalysis,
        result: LLMResult,
    ) -> None:
        data = analysis.model_dump()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE business_articles
                SET analysis_status = 'success', category = ?,
                    secondary_categories = ?, summary = ?,
                    impact_direction = ?, impact_score = ?,
                    impact_analysis = ?, risk_level = ?, risk_score = ?,
                    risk_factors = ?, opportunities = ?,
                    recommended_actions = ?, analysis_evidence = ?,
                    model = ?, prompt_version = ?, raw_response = ?,
                    prompt_tokens = ?, completion_tokens = ?, analyzed_at = ?,
                    error_message = ''
                WHERE article_id = ?
                """,
                (
                    analysis.category,
                    json.dumps(data["secondary_categories"], ensure_ascii=False),
                    analysis.summary,
                    analysis.impact_direction,
                    analysis.impact_score,
                    analysis.impact_analysis,
                    analysis.risk_level,
                    analysis.risk_score,
                    json.dumps(data["risk_factors"], ensure_ascii=False),
                    json.dumps(data["opportunities"], ensure_ascii=False),
                    json.dumps(data["recommended_actions"], ensure_ascii=False),
                    json.dumps(data["evidence"], ensure_ascii=False),
                    result.model,
                    BUSINESS_ANALYSIS_PROMPT_VERSION,
                    result.raw_content[:50000],
                    result.prompt_tokens,
                    result.completion_tokens,
                    utc_now_iso(),
                    article_id,
                ),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'success', is_relevant = 1,
                    business_analysis_status = 'success', error_message = ''
                WHERE run_id = ? AND article_id = ?
                """,
                (run_id, article_id),
            )

    def fail_business_analysis(
        self, run_id: int, article_id: int, message: str
    ) -> None:
        error = message[:2000]
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE business_articles
                SET analysis_status = 'failed', analyzed_at = ?,
                    error_message = ?
                WHERE article_id = ?
                """,
                (utc_now_iso(), error, article_id),
            )
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed', is_relevant = 1,
                    business_analysis_status = 'failed', error_message = ?
                WHERE run_id = ? AND article_id = ?
                """,
                (error, run_id, article_id),
            )

    def fail_run_item(self, run_id: int, article_id: int, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE ai_analysis_run_items
                SET status = 'failed', error_message = ?
                WHERE run_id = ? AND article_id = ?
                """,
                (message[:2000], run_id, article_id),
            )

    def finish_analysis_run(
        self,
        run_id: int,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        message: str = "",
    ) -> None:
        with self.database.connect() as connection:
            counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status IN ('pending', 'processing')
                        THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN is_relevant = 1 THEN 1 ELSE 0 END) AS relevant,
                    SUM(CASE WHEN status = 'success' AND is_relevant = 0
                        THEN 1 ELSE 0 END) AS irrelevant,
                    COUNT(*) AS total
                FROM ai_analysis_run_items
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            succeeded = int(counts["succeeded"] or 0)
            failed = int(counts["failed"] or 0)
            pending = int(counts["pending"] or 0)
            total = int(counts["total"] or 0)
            if pending:
                status = "partial"
            elif failed == 0:
                status = "success"
            elif succeeded == 0:
                status = "failed"
            else:
                status = "partial"
            if not message and total == 0:
                message = "没有待处理文章"
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

    def get_analysis_run(self, run_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            run_row = connection.execute(
                "SELECT * FROM ai_analysis_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None
            item_rows = connection.execute(
                """
                SELECT ari.*, a.title, a.url, ic.final_url, ic.content_chars,
                       rr.relevance_score, rr.relevance_reason,
                       ba.category, ba.risk_level, ba.risk_score
                FROM ai_analysis_run_items ari
                JOIN articles a ON a.id = ari.article_id
                LEFT JOIN article_contents ic ON ic.article_id = a.id
                LEFT JOIN article_relevance_reviews rr ON rr.article_id = a.id
                LEFT JOIN business_articles ba ON ba.article_id = a.id
                WHERE ari.run_id = ?
                ORDER BY a.id
                """,
                (run_id,),
            ).fetchall()
        run = dict(run_row)
        run["items"] = [dict(row) for row in item_rows]
        return run

    def list_reviews(
        self,
        *,
        relevant: bool | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = [
            "rr.status = 'success'",
            "ic.status = 'success'",
            "rr.content_hash = ic.content_hash",
        ]
        parameters: list[Any] = []
        if relevant is not None:
            filters.append("rr.is_relevant = ?")
            parameters.append(int(relevant))
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
                    FROM article_relevance_reviews rr
                    JOIN articles a ON a.id = rr.article_id
                    JOIN article_contents ic ON ic.article_id = rr.article_id
                    {where}
                    """,  # noqa: S608
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT rr.*, a.title, a.url, a.publisher, a.published_at,
                       ic.final_url, ic.content_chars, ic.fetched_at
                FROM article_relevance_reviews rr
                JOIN articles a ON a.id = rr.article_id
                JOIN article_contents ic ON ic.article_id = rr.article_id
                {where}
                ORDER BY rr.reviewed_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                [*parameters, limit, offset],
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.REVIEW_JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
        return {"total": total, "items": items}

    def list_business_articles(
        self,
        *,
        category: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = [
            "ba.analysis_status = 'success'",
            "ic.status = 'success'",
            "ba.content_hash = ic.content_hash",
        ]
        parameters: list[Any] = []
        if category:
            filters.append("ba.category = ?")
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
                    FROM business_articles ba
                    JOIN articles a ON a.id = ba.article_id
                    JOIN article_contents ic ON ic.article_id = ba.article_id
                    {where}
                    """,  # noqa: S608
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT ba.*, a.title, a.url, a.publisher, a.published_at,
                       a.collected_at, ic.final_url, ic.content_chars,
                       ic.fetched_at
                FROM business_articles ba
                JOIN articles a ON a.id = ba.article_id
                JOIN article_contents ic ON ic.article_id = ba.article_id
                {where}
                ORDER BY a.published_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608
                [*parameters, limit, offset],
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.BUSINESS_JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
        return {"total": total, "items": items}

    def relevant_articles_for_report(
        self, report_date: date, keyword_category_id: int
    ) -> list[dict[str, Any]]:
        window_start, window_end = local_date_window(report_date)
        filters = [
            "ba.analysis_status = 'success'",
            "ic.status = 'success'",
            "ba.content_hash = ic.content_hash",
            "a.published_at >= ?",
            "a.published_at < ?",
        ]
        parameters: list[Any] = [
            keyword_category_id,
            window_start,
            window_end,
        ]
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                WITH category_matches AS (
                    SELECT
                        ak.article_id,
                        GROUP_CONCAT(DISTINCT k.name) AS matched_keyword_names
                    FROM article_keywords ak
                    JOIN keywords k ON k.id = ak.keyword_id
                    WHERE k.category_id = ?
                    GROUP BY ak.article_id
                )
                SELECT ba.*, a.title, a.url, a.publisher, a.published_at,
                       ic.final_url, cm.matched_keyword_names,
                       rr.keyword_categories AS reviewed_keyword_categories,
                       rr.prompt_version AS relevance_prompt_version
                FROM business_articles ba
                JOIN articles a ON a.id = ba.article_id
                JOIN article_contents ic ON ic.article_id = ba.article_id
                JOIN article_relevance_reviews rr
                  ON rr.article_id = ba.article_id
                JOIN category_matches cm ON cm.article_id = ba.article_id
                WHERE {' AND '.join(filters)}
                ORDER BY ba.risk_score DESC, a.published_at DESC
                """,  # noqa: S608
                parameters,
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.BUSINESS_JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
            item["reviewed_keyword_categories"] = self._decode_json(
                item["reviewed_keyword_categories"], []
            )
        category = self.database.get_keyword_category(keyword_category_id)
        category_name = str(category["name"]) if category else ""
        required_codes = KEYWORD_CATEGORY_BUSINESS_CODES.get(category_name)
        if not required_codes:
            return items
        return [
            item
            for item in items
            if (
                category_name in item["reviewed_keyword_categories"]
                if item["relevance_prompt_version"]
                in {"intco-relevance-v7", RELEVANCE_PROMPT_VERSION}
                else bool(
                    required_codes
                    & {
                        str(item["category"]),
                        *(str(value) for value in item["secondary_categories"]),
                    }
                )
            )
        ]

    def create_report(
        self,
        *,
        report_date: date,
        keyword_category_id: int,
        keyword_category_name: str,
        article_ids: list[int],
        model: str,
    ) -> int:
        now = utc_now_iso()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO daily_reports
                    (report_date, categories, keyword_category_id,
                     keyword_category_name, status, article_count, model,
                     prompt_version, created_at, updated_at)
                VALUES (?, '[]', ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    report_date.isoformat(),
                    keyword_category_id,
                    keyword_category_name,
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
                       a.published_at, COALESCE(ba.category, 'other') AS category,
                       COALESCE(ba.summary, '') AS summary,
                       COALESCE(ba.impact_analysis, '') AS impact_analysis,
                       COALESCE(ba.risk_level, 'low') AS risk_level,
                       COALESCE(ba.risk_score, 0) AS risk_score,
                       ic.final_url
                FROM daily_report_articles dra
                JOIN articles a ON a.id = dra.article_id
                LEFT JOIN business_articles ba ON ba.article_id = a.id
                LEFT JOIN article_contents ic ON ic.article_id = a.id
                WHERE dra.report_id = ?
                ORDER BY ba.risk_score DESC, a.published_at DESC
                """,
                (report_id,),
            ).fetchall()
        report = dict(row)
        for field in self.REPORT_JSON_FIELDS:
            report[field] = self._decode_json(report[field], [])
        articles = [dict(article) for article in article_rows]
        sources_by_id: dict[int, dict[str, Any]] = {}
        for article in articles:
            article["source_url"] = article.get("final_url") or article["url"]
            source = {
                "article_id": int(article["article_id"]),
                "title": article["title"],
                "publisher": article["publisher"],
                "source_url": article["source_url"],
            }
            sources_by_id[int(article["article_id"])] = source
        report["articles"] = articles
        report["sources"] = list(sources_by_id.values())
        for development in report["key_developments"]:
            if not isinstance(development, dict):
                continue
            source = sources_by_id.get(int(development.get("article_id") or 0))
            development["sources"] = [source] if source else []
        for field in (
            "key_risks",
            "opportunities",
            "recommended_actions",
            "watchlist",
        ):
            for item in report[field]:
                if not isinstance(item, dict):
                    continue
                item["sources"] = [
                    sources_by_id[article_id]
                    for article_id in dict.fromkeys(
                        int(value)
                        for value in item.get("article_ids", [])
                        if str(value).isdigit()
                    )
                    if article_id in sources_by_id
                ]
        return report

    def has_successful_report(
        self, report_date: date, keyword_category_id: int
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_reports
                WHERE report_date = ?
                  AND keyword_category_id = ?
                  AND status = 'success'
                LIMIT 1
                """,
                (report_date.isoformat(), keyword_category_id),
            ).fetchone()
        return row is not None


class ArticleAnalysisManager:
    def __init__(
        self,
        database: Database,
        repository: IntelligenceRepository,
        client: JSONLLMClient,
        content_reader: ArticleContentReader,
    ) -> None:
        self.database = database
        self.repository = repository
        self.client = client
        self.content_reader = content_reader
        self._state_lock = threading.Lock()
        self._running_run_id: int | None = None
        self._pause_requested = threading.Event()

    @property
    def running_run_id(self) -> int | None:
        with self._state_lock:
            return self._running_run_id

    @property
    def pause_requested(self) -> bool:
        with self._state_lock:
            return (
                self._running_run_id is not None
                and self._pause_requested.is_set()
            )

    def request_pause(self) -> int | None:
        with self._state_lock:
            if self._running_run_id is None:
                return None
            self._pause_requested.set()
            return self._running_run_id

    def prepare(
        self,
        *,
        trigger_type: str = "manual",
        limit: int = 20,
        force: bool = False,
        article_ids: list[int] | None = None,
        collection_run_id: int | None = None,
    ) -> tuple[int, list[int]]:
        if not self.content_reader.configured:
            raise ValueError("尚未配置 OPENAI_API_KEY")
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        with self._state_lock:
            if self._running_run_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"AI 处理任务 #{self._running_run_id} 正在运行"
                )
            self._pause_requested.clear()
            candidate_ids = self.repository.candidate_article_ids(
                limit=limit,
                force=force,
                article_ids=article_ids,
                collection_run_id=collection_run_id,
            )
            run_id = self.repository.create_analysis_run(
                candidate_ids, trigger_type=trigger_type, model=self.client.model
            )
            self._running_run_id = run_id
        return run_id, candidate_ids

    def prepare_queue(
        self,
        *,
        trigger_type: str = "manual",
        batch_size: int = 20,
        force: bool = False,
        article_ids: list[int] | None = None,
        collection_run_id: int | None = None,
    ) -> tuple[int, list[int]]:
        if not self.content_reader.configured:
            raise ValueError("尚未配置 OPENAI_API_KEY")
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        batch_size = max(1, min(100, batch_size))
        with self._state_lock:
            if self._running_run_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"AI 处理任务 #{self._running_run_id} 正在运行"
                )
            self._pause_requested.clear()
            candidate_ids = self.repository.candidate_article_ids(
                limit=None,
                force=force,
                article_ids=article_ids,
                collection_run_id=collection_run_id,
            )
            run_id = self.repository.create_analysis_run(
                candidate_ids[:batch_size],
                trigger_type=trigger_type,
                model=self.client.model,
            )
            self._running_run_id = run_id
        return run_id, candidate_ids

    def execute(
        self,
        run_id: int,
        article_ids: list[int],
        *,
        force: bool = False,
        refresh_content: bool = False,
    ) -> None:
        try:
            self._execute_run(
                run_id,
                article_ids,
                force=force,
                refresh_content=refresh_content,
            )
        finally:
            with self._state_lock:
                if self._running_run_id == run_id:
                    self._running_run_id = None
                    self._pause_requested.clear()

    def execute_queue(
        self,
        first_run_id: int,
        article_ids: list[int],
        *,
        batch_size: int = 20,
        trigger_type: str = "manual",
        force: bool = False,
        refresh_content: bool = False,
    ) -> None:
        batch_size = max(1, min(100, batch_size))
        batches = [
            article_ids[index : index + batch_size]
            for index in range(0, len(article_ids), batch_size)
        ] or [[]]
        active_run_id = first_run_id
        try:
            for index, batch in enumerate(batches):
                if index > 0:
                    if self._pause_requested.is_set():
                        break
                    active_run_id = self.repository.create_analysis_run(
                        batch,
                        trigger_type=trigger_type,
                        model=self.client.model,
                    )
                    with self._state_lock:
                        self._running_run_id = active_run_id
                should_continue = self._execute_run(
                    active_run_id,
                    batch,
                    force=force,
                    refresh_content=refresh_content,
                )
                if not should_continue or self._pause_requested.is_set():
                    break
        finally:
            with self._state_lock:
                if self._running_run_id == active_run_id:
                    self._running_run_id = None
                    self._pause_requested.clear()

    def _execute_run(
        self,
        run_id: int,
        article_ids: list[int],
        *,
        force: bool = False,
        refresh_content: bool = False,
    ) -> bool:
        prompt_tokens = 0
        completion_tokens = 0
        should_continue = True
        run_message = ""
        try:
            settings = self.database.get_settings()
            business_profile = settings.get("ai_business_profile", "")
            relevance_prompt = settings.get(
                "ai_relevance_prompt", DEFAULT_RELEVANCE_PROMPT
            )
            threshold = self._integer_setting(
                settings.get("ai_relevance_threshold", "70"), 70, 0, 100
            )
            max_content_chars = self._integer_setting(
                settings.get("ai_content_max_chars", "30000"),
                30000,
                2000,
                100000,
            )
            for article_id in article_ids:
                if self._pause_requested.is_set():
                    should_continue = False
                    run_message = "用户已暂停处理；未开始文章保留为待处理"
                    break
                article = self.repository.get_article(article_id)
                if article is None:
                    self.repository.fail_run_item(
                        run_id, article_id, "文章不存在"
                    )
                    continue
                try:
                    content = self._get_or_read_content(
                        run_id,
                        article,
                        refresh_content=refresh_content,
                    )
                except _ProviderRateLimited as exc:
                    should_continue = False
                    run_message = str(exc)
                    break
                if content is None:
                    continue

                review_row = self.repository.get_review(article_id)
                review: RelevanceAssessment
                review_is_current = bool(
                    review_row
                    and review_row["status"] == "success"
                    and review_row["content_hash"] == content["content_hash"]
                )
                if force or not review_is_current:
                    try:
                        self.repository.mark_relevance_processing(
                            run_id,
                            article_id,
                            model=self.client.model,
                            content_hash=content["content_hash"],
                        )
                        prompt_article = self._prompt_article(
                            article, content, max_content_chars
                        )
                        system_prompt, user_prompt = build_relevance_prompts(
                            prompt_article,
                            business_profile,
                            relevance_prompt,
                        )
                        result = self.client.complete_json(
                            system_prompt, user_prompt, max_tokens=900
                        )
                        review = RelevanceAssessment.model_validate(result.data)
                        candidate_keyword_categories = {
                            str(value)
                            for value in prompt_article[
                                "matched_keyword_categories"
                            ]
                        }
                        review = review.model_copy(
                            update={
                                "keyword_categories": [
                                    value
                                    for value in review.keyword_categories
                                    if value in candidate_keyword_categories
                                ]
                            }
                        )
                        review = enforce_company_fact_boundary(
                            full_text=prompt_article["full_text"],
                            review=review,
                        )
                        if review.is_relevant and review.relevance_score < threshold:
                            threshold_reason = (
                                f"{review.relevance_reason.rstrip('。')}；"
                                f"相关性分数 {review.relevance_score} 低于系统阈值 "
                                f"{threshold}，按无关处理。"
                            )
                            review = review.model_copy(
                                update={
                                    "is_relevant": False,
                                    "relevance_reason": threshold_reason[:1000],
                                    "category": "other",
                                    "secondary_categories": [],
                                    "keyword_categories": [],
                                }
                            )
                        self.repository.save_relevance(
                            run_id,
                            article_id,
                            review,
                            result,
                            content_hash=content["content_hash"],
                        )
                        prompt_tokens += result.prompt_tokens
                        completion_tokens += result.completion_tokens
                    except Exception as exc:
                        self.repository.fail_relevance(
                            run_id,
                            article_id,
                            f"相关性审核失败: {type(exc).__name__}: {exc}",
                            content_hash=content["content_hash"],
                        )
                        continue
                else:
                    review = RelevanceAssessment.model_validate(
                        {
                            "is_relevant": bool(review_row["is_relevant"]),
                            "relevance_score": review_row["relevance_score"],
                            "relevance_reason": review_row["relevance_reason"],
                            "category": review_row["category"],
                            "secondary_categories": review_row[
                                "secondary_categories"
                            ],
                            "keyword_categories": review_row[
                                "keyword_categories"
                            ],
                            "evidence": review_row["evidence"],
                            "confidence": review_row["confidence"],
                        }
                    )
                    self.repository.reuse_relevance(
                        run_id,
                        article_id,
                        is_relevant=review.is_relevant,
                    )

                if not review.is_relevant:
                    continue
                try:
                    self.repository.mark_business_processing(
                        run_id,
                        article_id,
                        review,
                        content_hash=content["content_hash"],
                        model=self.client.model,
                    )
                    prompt_article = self._prompt_article(
                        article, content, max_content_chars
                    )
                    system_prompt, user_prompt = build_business_analysis_prompts(
                        article=prompt_article,
                        relevance_review=review.model_dump(),
                        business_profile=business_profile,
                    )
                    result = self.client.complete_json(
                        system_prompt, user_prompt, max_tokens=1800
                    )
                    analysis = BusinessAnalysis.model_validate(result.data)
                    analysis = enforce_company_fact_boundary(
                        full_text=prompt_article["full_text"],
                        analysis=analysis,
                    )
                    analysis = analysis.model_copy(
                        update={
                            "risk_level": risk_level_for_score(
                                analysis.risk_score
                            )
                        }
                    )
                    self.repository.save_business_analysis(
                        run_id, article_id, analysis, result
                    )
                    prompt_tokens += result.prompt_tokens
                    completion_tokens += result.completion_tokens
                except Exception as exc:
                    self.repository.fail_business_analysis(
                        run_id,
                        article_id,
                        f"业务分析失败: {type(exc).__name__}: {exc}",
                    )
            if should_continue and self._pause_requested.is_set():
                should_continue = False
                run_message = "用户已暂停处理；未开始文章保留为待处理"
        finally:
            self.repository.finish_analysis_run(
                run_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                message=run_message,
            )
        return should_continue

    def _get_or_read_content(
        self,
        run_id: int,
        article: dict[str, Any],
        *,
        refresh_content: bool,
    ) -> dict[str, Any] | None:
        article_id = int(article["id"])
        existing = self.repository.get_content(article_id)
        if existing and existing["status"] == "success" and not refresh_content:
            self.repository.reuse_content(run_id, article_id)
            return existing
        urls = self._content_urls(article)
        requested_url = urls[0] if urls else article["url"]
        self.repository.mark_content_processing(
            run_id, article_id, requested_url
        )
        try:
            document = self.content_reader.read(
                ArticleReference(
                    title=str(article.get("title") or ""),
                    publisher=str(article.get("publisher") or ""),
                    urls=tuple(urls),
                )
            )
            self.repository.save_content(run_id, article_id, document)
            return self.repository.get_content(article_id)
        except Exception as exc:
            message = f"大模型网页读取失败: {type(exc).__name__}: {exc}"
            if isinstance(exc, ContentFetchError):
                failure_kind = exc.failure_kind
                retryable = exc.retryable
            else:
                failure_kind = "unexpected"
                retryable = True
        self.repository.fail_content(
            run_id,
            article_id,
            requested_url,
            message,
            failure_kind=failure_kind,
            retryable=retryable,
        )
        if failure_kind == "openai_http_429":
            raise _ProviderRateLimited(
                "CCTQ/OpenAI 网页读取限流，本批次已暂停；剩余文章保留为待处理"
            )
        if failure_kind == "openai_web_search_unavailable":
            raise _ProviderRateLimited(
                "CCTQ/OpenAI 网页搜索服务不可用，本批次已暂停；剩余文章保留为待处理"
            )
        if failure_kind == "llm_http_429":
            raise _ProviderRateLimited(
                "DeepSeek 网页读取限流，本批次已暂停；剩余文章保留为待处理"
            )
        if failure_kind == "llm_web_search_unavailable":
            raise _ProviderRateLimited(
                "DeepSeek 网页搜索服务不可用，本批次已暂停；剩余文章保留为待处理"
            )
        return None

    @staticmethod
    def _content_urls(article: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for source in article.get("sources", []):
            candidates.extend(
                [source.get("observed_url", ""), source.get("canonical_url", "")]
            )
        candidates.extend([article.get("canonical_url", ""), article.get("url", "")])
        unique: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            url = str(value or "").strip()
            key = url.casefold()
            if url and key not in seen:
                seen.add(key)
                unique.append(url)
        return sorted(
            unique,
            key=lambda value: (
                (urlsplit(value).hostname or "").casefold() == "news.google.com",
                unique.index(value),
            ),
        )

    @staticmethod
    def _prompt_article(
        article: dict[str, Any], content: dict[str, Any], max_chars: int
    ) -> dict[str, Any]:
        full_text = str(content["full_text"])
        return {
            "article_id": article["id"],
            "title": article["title"],
            "publisher": article["publisher_normalized"] or article["publisher"],
            "published_at": article["published_at"],
            "final_url": content["final_url"],
            "content_hash": content["content_hash"],
            "content_chars": content["content_chars"],
            "content_truncated_for_model": len(full_text) > max_chars,
            "matched_keyword_categories": sorted(
                {
                    str(keyword["category_name"])
                    for keyword in article.get("keywords", [])
                    if keyword.get("category_name")
                }
            ),
            "full_text": full_text[:max_chars],
        }

    @staticmethod
    def _integer_setting(value: str, fallback: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return fallback


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
        self, report_date: date, keyword_category_id: int
    ) -> tuple[int, str, list[dict[str, Any]]]:
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        keyword_category = self.database.get_keyword_category(
            keyword_category_id
        )
        if keyword_category is None:
            raise ValueError("未知或已停用的关键词分类")
        keyword_category_name = str(keyword_category["name"])
        with self._state_lock:
            if self._running_report_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"日报 #{self._running_report_id} 正在生成"
                )
            articles = self.repository.relevant_articles_for_report(
                report_date, keyword_category_id
            )
            if not articles:
                raise ValueError(
                    f"“{keyword_category_name}”分类在所选日期下"
                    "没有完成全文审核与业务分析的相关新闻"
                )
            report_id = self.repository.create_report(
                report_date=report_date,
                keyword_category_id=keyword_category_id,
                keyword_category_name=keyword_category_name,
                article_ids=[int(article["article_id"]) for article in articles],
                model=self.client.model,
            )
            self._running_report_id = report_id
        return report_id, keyword_category_name, articles

    def execute(
        self,
        report_id: int,
        report_date: date,
        keyword_category_name: str,
        articles: list[dict[str, Any]],
    ) -> None:
        try:
            settings = self.database.get_settings()
            report_articles = [self._report_article(article) for article in articles]
            category_prompt_setting = REPORT_CATEGORY_SETTING_KEYS.get(
                keyword_category_name
            )
            default_category_prompt = DEFAULT_REPORT_CATEGORY_PROMPTS.get(
                keyword_category_name, ""
            )
            category_report_prompt = (
                settings.get(category_prompt_setting, default_category_prompt)
                if category_prompt_setting
                else default_category_prompt
            )
            system_prompt, user_prompt = build_report_prompts(
                report_date=report_date.isoformat(),
                keyword_category_name=keyword_category_name,
                articles=report_articles,
                business_profile=settings.get("ai_business_profile", ""),
                report_prompt=settings.get(
                    "ai_report_prompt", DEFAULT_REPORT_PROMPT
                ),
                category_report_prompt=category_report_prompt,
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
            cited_updates = {
                field: self._valid_cited_items(
                    getattr(assessment, field), valid_article_ids
                )
                for field in (
                    "key_risks",
                    "opportunities",
                    "recommended_actions",
                    "watchlist",
                )
            }
            article_floor = max(int(article["risk_score"]) for article in articles)
            risk_score = max(assessment.risk_score, article_floor)
            assessment = assessment.model_copy(
                update={
                    "key_developments": developments,
                    **cited_updates,
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
            "source_url": article.get("final_url") or article["url"],
            "matched_keywords": [
                name.strip()
                for name in str(
                    article.get("matched_keyword_names") or ""
                ).split(",")
                if name.strip()
            ],
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
            "evidence": article["analysis_evidence"],
        }

    @staticmethod
    def _valid_cited_items(
        items: list[CitedReportItem], valid_article_ids: set[int]
    ) -> list[CitedReportItem]:
        result: list[CitedReportItem] = []
        for item in items:
            article_ids = [
                article_id
                for article_id in item.article_ids
                if article_id in valid_article_ids
            ]
            if not article_ids:
                continue
            result.append(item.model_copy(update={"article_ids": article_ids}))
        return result


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
        if (
            not self.analysis_manager.client.configured
            or not self.analysis_manager.content_reader.configured
        ):
            return
        try:
            limit = max(1, min(100, int(settings.get("ai_batch_size", "20"))))
        except ValueError:
            limit = 20
        try:
            run_id, article_ids = self.analysis_manager.prepare_queue(
                trigger_type="collection", batch_size=limit
            )
            self.analysis_manager.execute_queue(
                run_id,
                article_ids,
                batch_size=limit,
                trigger_type="collection",
            )
        except IntelligenceAlreadyRunningError:
            return
        if settings.get("ai_auto_report", "false").lower() != "true":
            return
        timezone = ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
        report_date = datetime.now(timezone).date()
        for keyword_category in self.database.get_keyword_categories():
            category_id = int(keyword_category["id"])
            if self.repository.has_successful_report(report_date, category_id):
                continue
            try:
                report_id, category_name, articles = self.report_manager.prepare(
                    report_date, category_id
                )
                self.report_manager.execute(
                    report_id, report_date, category_name, articles
                )
            except IntelligenceAlreadyRunningError:
                return
            except ValueError:
                continue
