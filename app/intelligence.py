from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
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
from .feishu import FeishuWebhookClient
from .llm import JSONLLMClient, LLMResult
from .prompts import (
    BUSINESS_ANALYSIS_PROMPT_VERSION,
    CATEGORY_LABELS,
    DEFAULT_RELEVANCE_PROMPT,
    DEFAULT_REPORT_PROMPT,
    RELEVANCE_PROMPT_VERSION,
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

_REPORT_REGION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("全球", (r"全球", r"全世界", r"\bglobal(?:ly)?\b", r"\bworldwide\b")),
    ("欧盟", (r"欧盟", r"\bEuropean Union\b", r"\bEU(?:'s)?\b")),
    ("欧洲", (r"欧洲", r"\bEurope(?:an)?\b")),
    ("美国", (r"美国", r"\bUnited States\b", r"\bU\.S\.(?:A\.)?\b", r"(?-i:\bUS(?:A)?\b)", r"\bAmerican\b")),
    ("中国", (r"中国", r"\bChina\b", r"\bChinese\b")),
    ("德国", (r"德国", r"\bGerman(?:y)?\b")),
    ("法国", (r"法国", r"\bFrance\b", r"\bFrench\b")),
    ("意大利", (r"意大利", r"\bItal(?:y|ian)\b")),
    ("西班牙", (r"西班牙", r"\bSpain\b", r"\bSpanish\b")),
    ("荷兰", (r"荷兰", r"\bNetherlands\b", r"\bDutch\b")),
    ("波兰", (r"波兰", r"\bPoland\b", r"\bPolish\b")),
    ("新加坡", (r"新加坡", r"\bSingapore(?:an)?\b")),
    ("菲律宾", (r"菲律宾", r"\bPhilippines?\b", r"\bFilipino\b")),
    ("中国台湾地区", (r"台湾", r"\bTaiwan(?:ese)?\b")),
    ("中国香港地区", (r"香港", r"\bHong Kong\b")),
    ("马来西亚", (r"马来西亚", r"\bMalaysia(?:n)?\b")),
    ("泰国", (r"泰国", r"\bThailand\b", r"\bThai\b")),
    ("越南", (r"越南", r"\bVietnam(?:ese)?\b")),
    ("印度尼西亚", (r"印度尼西亚", r"印尼", r"\bIndonesia(?:n)?\b")),
    ("印度", (r"印度", r"\bIndia(?:n)?\b")),
    ("日本", (r"日本", r"\bJapan(?:ese)?\b")),
    ("韩国", (r"韩国", r"\bSouth Korea(?:n)?\b", r"\bKorea(?:n)?\b")),
    ("英国", (r"英国", r"\bUnited Kingdom\b", r"(?-i:\bUK\b)", r"\bBritain\b", r"\bBritish\b")),
    ("加拿大", (r"加拿大", r"\b(?:Canada|Canadian)\b")),
    ("墨西哥", (r"墨西哥", r"\b(?:Mexico|Mexican)\b")),
    ("巴西", (r"巴西", r"\bBrazil(?:ian)?\b")),
    ("澳大利亚", (r"澳大利亚", r"澳洲", r"\bAustralia(?:n)?\b")),
    ("俄罗斯", (r"俄罗斯", r"\bRussia(?:n)?\b")),
    ("乌克兰", (r"乌克兰", r"\b(?:Ukraine|Ukrainian)\b")),
    ("土耳其", (r"土耳其", r"\bT(?:u|ü)rkiye\b", r"\b(?:Turkey|Turkish)\b")),
    ("沙特阿拉伯", (r"沙特", r"\bSaudi Arabia(?:n)?\b")),
    ("阿联酋", (r"阿联酋", r"\bUnited Arab Emirates\b", r"\bUAE\b")),
    ("南非", (r"南非", r"\bSouth Africa(?:n)?\b")),
    ("东盟", (r"东盟", r"\bASEAN\b")),
    ("亚太地区", (r"亚太", r"\bAsia[- ]Pacific\b", r"\bAPAC\b")),
    ("中东", (r"中东", r"\bMiddle East(?:ern)?\b")),
    ("拉丁美洲", (r"拉丁美洲", r"拉美", r"\bLatin America(?:n)?\b")),
    ("非洲", (r"非洲", r"\bAfrica(?:n)?\b")),
)

_REPORT_PRODUCT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("丁腈手套", (r"丁腈手套", r"\bnitrile (?:exam(?:ination)? |medical |disposable )?gloves?\b")),
    ("PVC手套", (r"PVC手套", r"聚氯乙烯手套", r"\bPVC gloves?\b", r"\bvinyl gloves?\b")),
    ("PE手套", (r"PE手套", r"聚乙烯手套", r"\bpolyethylene gloves?\b", r"\bPE gloves?\b")),
    ("乳胶手套", (r"乳胶手套", r"\blatex gloves?\b")),
    ("医用检查手套", (r"医用检查手套", r"检查手套", r"\bmedical exam(?:ination)? gloves?\b")),
    ("外科手套", (r"外科手套", r"手术手套", r"\bsurgical gloves?\b")),
    ("一次性手套", (r"一次性(?:丁腈|PVC|PE|乳胶)?手套", r"医用手套", r"医疗手套", r"\bdisposable gloves?\b", r"\bmedical gloves?\b")),
    ("手套", (r"手套", r"\bgloves?\b")),
    ("口罩", (r"口罩", r"\bface masks?\b", r"\bmedical masks?\b", r"\brespirators?\b")),
    ("隔离衣", (r"隔离衣", r"\bisolation gowns?\b")),
    ("防护服", (r"防护服", r"\bprotective clothing\b", r"\bPPE suits?\b")),
    ("丁腈胶乳（NBR）", (r"丁腈胶乳", r"\bnitrile butadiene rubber\b", r"\bNBR\b")),
    ("PVC原料", (r"PVC原料", r"聚氯乙烯原料", r"\bPVC resin\b")),
    ("PE原料", (r"PE原料", r"聚乙烯原料", r"\bpolyethylene resin\b")),
    ("轮椅", (r"轮椅", r"\bwheelchairs?\b")),
    ("代步车", (r"代步车", r"\bmobility scooters?\b")),
    ("助行器", (r"助行器", r"\bwalkers?\b", r"\bwalking aids?\b")),
    ("冷热敷产品", (r"冷热敷", r"\bhot and cold packs?\b", r"\bcold packs?\b")),
    ("急救产品", (r"急救产品", r"急救包", r"\bfirst[- ]aid (?:products?|kits?)\b")),
)


def _dimension_matches(
    text: str, patterns: tuple[tuple[str, tuple[str, ...]], ...]
) -> list[str]:
    return [
        label
        for label, aliases in patterns
        if any(re.search(alias, text, re.IGNORECASE) for alias in aliases)
    ]


def _dimension_text(article: dict[str, Any], *, include_full_text: bool) -> str:
    values: list[str] = []
    for field in (
        "title",
        "summary",
        "impact_analysis",
        "matched_keyword_names",
    ):
        value = str(article.get(field) or "").strip()
        if value:
            values.append(value)
    for field in ("risk_factors", "analysis_evidence", "evidence"):
        value = article.get(field) or []
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
    if include_full_text:
        values.append(str(article.get("full_text") or "")[:50_000])
    return "\n".join(values)


def extract_report_dimensions(article: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Extract explicit impact-region and product candidates without guessing."""
    primary_text = _dimension_text(article, include_full_text=False)
    regions = _dimension_matches(primary_text, _REPORT_REGION_PATTERNS)
    products = _dimension_matches(primary_text, _REPORT_PRODUCT_PATTERNS)
    if (not regions or not products) and article.get("full_text"):
        full_text = _dimension_text(article, include_full_text=True)
        if not regions:
            regions = _dimension_matches(full_text, _REPORT_REGION_PATTERNS)
        if not products:
            products = _dimension_matches(full_text, _REPORT_PRODUCT_PATTERNS)
    if "全球" in regions:
        regions = ["全球"]
    elif "欧盟" in regions and "欧洲" in regions:
        regions.remove("欧洲")
    if len(products) > 1 and "手套" in products:
        products.remove("手套")
    return regions[:5], products[:5]


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


_REPORT_CATEGORY_ALIASES = {
    "cost_supply_chain": "raw_material_supply",
    "cost_and_supply_chain": "raw_material_supply",
    "supply_chain": "raw_material_supply",
    "competition_supply": "competitor",
    "competitive_supply": "competitor",
    "trade_policy_tariff": "trade_tariff",
    "trade_policy_and_tariff": "trade_tariff",
    "trade_policy": "policy_regulation",
    "industry_regulation": "policy_regulation",
}


def normalize_report_category(value: Any) -> str:
    """Map model-created report labels to a supported business category."""
    original = str(value or "").strip()
    normalized = re.sub(
        r"[^a-z0-9\u4e00-\u9fff]+", "_", original.casefold()
    ).strip("_")
    for code, label in CATEGORY_LABELS.items():
        if normalized in {code.casefold(), label.casefold()}:
            return code
    if normalized in _REPORT_CATEGORY_ALIASES:
        return _REPORT_CATEGORY_ALIASES[normalized]
    if "tariff" in normalized or "关税" in normalized:
        return "trade_tariff"
    if "compet" in normalized or "竞争" in normalized:
        return "competitor"
    if any(term in normalized for term in ("raw_material", "supply", "cost", "原材料", "供应链", "成本")):
        return "raw_material_supply"
    if any(term in normalized for term in ("regulation", "policy", "compliance", "法规", "政策", "合规")):
        return "policy_regulation"
    if any(term in normalized for term in ("demand", "market", "需求", "市场")):
        return "market_demand"
    if any(term in normalized for term in ("health", "卫生", "疫情")):
        return "public_health"
    if any(term in normalized for term in ("customer", "channel", "客户", "渠道")):
        return "customer_channel"
    if any(term in normalized for term in ("technology", "product", "技术", "产品")):
        return "technology_product"
    if "esg" in normalized or "可持续" in normalized:
        return "esg"
    return "other"


class KeyDevelopment(BaseModel):
    article_id: int
    category: CategoryCode
    title: str = Field(max_length=500)
    affected_region: str = Field(default="暂未明确", max_length=500)
    products: str = Field(default="暂未明确", max_length=500)
    risk_level: RiskLevel = "low"
    risk_score: int = Field(default=0, ge=0, le=100)
    finding: str = Field(max_length=1000)
    impact_reason: str = Field(default="暂未明确", max_length=1000)
    business_impact: str = Field(max_length=1000)
    recommended_action: str = Field(default="暂未明确", max_length=1000)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        return normalize_report_category(value)


class CitedReportItem(BaseModel):
    category: CategoryCode
    content: str = Field(min_length=1, max_length=1000)
    article_ids: list[int] = Field(default_factory=list, max_length=8)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        return normalize_report_category(value)

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
    key_developments: list[KeyDevelopment] = Field(default_factory=list, max_length=100)
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
        deleted = 0
        with self.database.connect() as connection:
            while True:
                rows = connection.execute(
                    f"""
                    SELECT a.id
                    FROM articles a
                    LEFT JOIN article_contents ic ON ic.article_id = a.id
                    LEFT JOIN article_relevance_reviews rr ON rr.article_id = a.id
                    LEFT JOIN business_articles ba ON ba.article_id = a.id
                    WHERE ({condition})
                    ORDER BY a.id
                    LIMIT 500
                    """  # noqa: S608
                ).fetchall()
                article_ids = [int(row["id"]) for row in rows]
                if not article_ids:
                    break
                placeholders = ",".join("?" for _ in article_ids)
                cursor = connection.execute(
                    f"DELETE FROM articles WHERE id IN ({placeholders})",  # noqa: S608
                    article_ids,
                )
                batch_deleted = int(cursor.rowcount)
                deleted += batch_deleted
                if batch_deleted == 0:
                    break
        return deleted

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
        self, report_date: date
    ) -> list[dict[str, Any]]:
        window_start, window_end = local_date_window(report_date)
        filters = [
            "ba.analysis_status = 'success'",
            "ic.status = 'success'",
            "ba.content_hash = ic.content_hash",
            "a.published_at >= ?",
            "a.published_at < ?",
        ]
        parameters: list[Any] = [window_start, window_end]
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                WITH keyword_matches AS (
                    SELECT
                        ak.article_id,
                        GROUP_CONCAT(DISTINCT k.name) AS matched_keyword_names
                    FROM article_keywords ak
                    JOIN keywords k ON k.id = ak.keyword_id
                    GROUP BY ak.article_id
                )
                SELECT ba.*, a.title, a.url, a.publisher, a.published_at,
                       ic.final_url, ic.full_text,
                       COALESCE(km.matched_keyword_names, '') AS matched_keyword_names
                FROM business_articles ba
                JOIN articles a ON a.id = ba.article_id
                JOIN article_contents ic ON ic.article_id = ba.article_id
                LEFT JOIN keyword_matches km ON km.article_id = ba.article_id
                WHERE {' AND '.join(filters)}
                ORDER BY ba.risk_score DESC, a.published_at DESC
                """,
                (window_start, window_end),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            for field in self.BUSINESS_JSON_FIELDS:
                item[field] = self._decode_json(item[field], [])
        return items

    def create_report(
        self,
        *,
        report_date: date,
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
                VALUES (?, '[]', 'running', ?, ?, ?, ?, ?)
                """,
                (
                    report_date.isoformat(),
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

    def delete_report(self, report_id: int) -> bool:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM daily_report_articles WHERE report_id = ?",
                (report_id,),
            )
            cursor = connection.execute(
                "DELETE FROM daily_reports WHERE id = ?",
                (report_id,),
            )
        return cursor.rowcount > 0

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
                       COALESCE(ba.risk_factors, '[]') AS risk_factors,
                       COALESCE(ba.recommended_actions, '[]') AS recommended_actions,
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
            article["risk_factors"] = self._decode_json(
                article.get("risk_factors"), []
            )
            article["recommended_actions"] = self._decode_json(
                article.get("recommended_actions"), []
            )
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

    def has_successful_report(self, report_date: date) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_reports
                WHERE report_date = ?
                  AND keyword_category_id IS NULL
                  AND status = 'success'
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
            parallelism = self._integer_setting(
                settings.get("ai_parallelism", "4"), 4, 1, 20
            )
            articles: list[dict[str, Any]] = []
            for article_id in article_ids:
                article = self.repository.get_article(article_id)
                if article is None:
                    self.repository.fail_run_item(
                        run_id, article_id, "文章不存在"
                    )
                    continue
                articles.append(article)

            stop_content = threading.Event()
            stop_message: list[str] = []
            stop_message_lock = threading.Lock()

            def read_content(
                article: dict[str, Any],
            ) -> tuple[dict[str, Any], dict[str, Any]] | None:
                if self._pause_requested.is_set() or stop_content.is_set():
                    return None
                try:
                    content = self._get_or_read_content(
                        run_id,
                        article,
                        refresh_content=refresh_content,
                    )
                except _ProviderRateLimited as exc:
                    with stop_message_lock:
                        if not stop_message:
                            stop_message.append(str(exc))
                    stop_content.set()
                    return None
                return (article, content) if content is not None else None

            content_items = [
                item
                for item in self._parallel_map(
                    articles,
                    read_content,
                    parallelism=parallelism,
                    thread_name_prefix="ai-content",
                )
                if item is not None
            ]

            def review_content(
                item: tuple[dict[str, Any], dict[str, Any]],
            ) -> tuple[
                dict[str, Any],
                dict[str, Any],
                RelevanceAssessment,
                int,
                int,
            ] | None:
                article, content = item
                article_id = int(article["id"])
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
                        return (
                            article,
                            content,
                            review,
                            result.prompt_tokens,
                            result.completion_tokens,
                        )
                    except Exception as exc:
                        self.repository.fail_relevance(
                            run_id,
                            article_id,
                            f"相关性审核失败: {type(exc).__name__}: {exc}",
                            content_hash=content["content_hash"],
                        )
                        return None
                review = RelevanceAssessment.model_validate(
                    {
                        "is_relevant": bool(review_row["is_relevant"]),
                        "relevance_score": review_row["relevance_score"],
                        "relevance_reason": review_row["relevance_reason"],
                        "category": review_row["category"],
                        "secondary_categories": review_row["secondary_categories"],
                        "keyword_categories": review_row["keyword_categories"],
                        "evidence": review_row["evidence"],
                        "confidence": review_row["confidence"],
                    }
                )
                self.repository.reuse_relevance(
                    run_id,
                    article_id,
                    is_relevant=review.is_relevant,
                )
                return article, content, review, 0, 0

            reviewed_items = [
                item
                for item in self._parallel_map(
                    content_items,
                    review_content,
                    parallelism=parallelism,
                    thread_name_prefix="ai-relevance",
                )
                if item is not None
            ]
            prompt_tokens += sum(item[3] for item in reviewed_items)
            completion_tokens += sum(item[4] for item in reviewed_items)

            relevant_items = [
                (article, content, review)
                for article, content, review, _, _ in reviewed_items
                if review.is_relevant
            ]

            def analyze_business(
                item: tuple[
                    dict[str, Any], dict[str, Any], RelevanceAssessment
                ],
            ) -> tuple[int, int]:
                article, content, review = item
                article_id = int(article["id"])
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
                    return result.prompt_tokens, result.completion_tokens
                except Exception as exc:
                    self.repository.fail_business_analysis(
                        run_id,
                        article_id,
                        f"业务分析失败: {type(exc).__name__}: {exc}",
                    )
                    return 0, 0

            business_token_usage = self._parallel_map(
                relevant_items,
                analyze_business,
                parallelism=parallelism,
                thread_name_prefix="ai-business",
            )
            prompt_tokens += sum(item[0] for item in business_token_usage)
            completion_tokens += sum(item[1] for item in business_token_usage)

            if stop_message:
                should_continue = False
                run_message = stop_message[0]
            elif self._pause_requested.is_set():
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

    @staticmethod
    def _parallel_map(
        items: list[Any],
        worker,
        *,
        parallelism: int,
        thread_name_prefix: str,
    ) -> list[Any]:
        if not items:
            return []
        max_workers = min(parallelism, len(items))
        if max_workers == 1:
            return [worker(item) for item in items]
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        ) as executor:
            return list(executor.map(worker, items))

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
            reference = ArticleReference(
                title=str(article.get("title") or ""),
                publisher=str(article.get("publisher") or ""),
                urls=tuple(urls),
                published_at=str(article.get("published_at") or ""),
            )
            try:
                document = self.content_reader.read(reference)
            except ContentFetchError as exc:
                if (
                    exc.retryable
                    and exc.failure_kind == "openai_web_unavailable"
                ):
                    document = self.content_reader.read(reference)
                else:
                    raise
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
        feishu_client: FeishuWebhookClient | None = None,
    ) -> None:
        self.database = database
        self.repository = repository
        self.client = client
        self.feishu_client = feishu_client or FeishuWebhookClient()
        self._state_lock = threading.Lock()
        self._running_report_id: int | None = None

    @property
    def running_report_id(self) -> int | None:
        with self._state_lock:
            return self._running_report_id

    def prepare(
        self, report_date: date
    ) -> tuple[int, list[dict[str, Any]]]:
        if not self.client.configured:
            raise ValueError("尚未配置 DEEPSEEK_API_KEY")
        with self._state_lock:
            if self._running_report_id is not None:
                raise IntelligenceAlreadyRunningError(
                    f"日报 #{self._running_report_id} 正在生成"
                )
            articles = self.repository.relevant_articles_for_report(report_date)
            if not articles:
                raise ValueError(
                    "所选日期下没有完成全文审核与业务分析的相关新闻"
                )
            report_id = self.repository.create_report(
                report_date=report_date,
                article_ids=[int(article["article_id"]) for article in articles],
                model=self.client.model,
            )
            self._running_report_id = report_id
        return report_id, articles

    def execute(
        self,
        report_id: int,
        report_date: date,
        articles: list[dict[str, Any]],
    ) -> None:
        try:
            settings = self.database.get_settings()
            report_articles = [self._report_article(article) for article in articles]
            system_prompt, user_prompt = build_report_prompts(
                report_date=report_date.isoformat(),
                articles=report_articles,
                business_profile=settings.get("ai_business_profile", ""),
                report_prompt=settings.get(
                    "ai_report_prompt", DEFAULT_REPORT_PROMPT
                ),
            )
            result = self.client.complete_json(
                system_prompt,
                user_prompt,
                max_tokens=max(3000, min(8000, 1800 + len(report_articles) * 320)),
            )
            assessment = DailyReportAssessment.model_validate(result.data)
            valid_article_ids = {int(article["article_id"]) for article in articles}
            developments_by_id = {
                development.article_id: development
                for development in assessment.key_developments
                if development.article_id in valid_article_ids
            }
            developments = [
                self._complete_development(
                    developments_by_id.get(int(article["article_id"])),
                    article,
                )
                for article in report_articles
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
                    "title": f"国际贸易市场情报日报（{report_date.isoformat()}）",
                    "risk_score": risk_score,
                    "risk_level": risk_level_for_score(risk_score),
                }
            )
            self.repository.save_report(report_id, assessment, result)
            self._send_report_to_feishu(report_id)
        except Exception as exc:
            self.repository.fail_report(report_id, f"{type(exc).__name__}: {exc}")
        finally:
            with self._state_lock:
                if self._running_report_id == report_id:
                    self._running_report_id = None

    def send_to_feishu(self, report_id: int) -> None:
        report = self.repository.get_report(report_id)
        if report is None:
            raise ValueError("日报不存在")
        if report["status"] != "success":
            raise ValueError("只有生成成功的日报可以推送到飞书")
        self.feishu_client.send_report(report)

    def _send_report_to_feishu(self, report_id: int) -> None:
        settings = self.database.get_settings()
        if (
            settings.get("feishu_auto_push", "false").lower() != "true"
            or not self.feishu_client.configured
        ):
            return
        try:
            self.send_to_feishu(report_id)
        except Exception:
            logging.getLogger(__name__).exception("日报 #%s 推送飞书失败", report_id)

    @staticmethod
    def _report_article(article: dict[str, Any]) -> dict[str, Any]:
        region_candidates, product_candidates = extract_report_dimensions(article)
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
            "region_candidates": region_candidates,
            "product_candidates": product_candidates,
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
    def _complete_development(
        development: KeyDevelopment | None,
        article: dict[str, Any],
    ) -> KeyDevelopment:
        risk_factors = [
            str(value).strip()
            for value in article.get("risk_factors", [])
            if str(value).strip()
        ]
        actions = [
            str(value).strip()
            for value in article.get("recommended_actions", [])
            if str(value).strip()
        ]
        defaults = {
            "article_id": int(article["article_id"]),
            "category": article.get("category") or "other",
            "title": str(article.get("title") or "未命名新闻")[:500],
            "affected_region": "、".join(
                article.get("region_candidates", [])
            )[:500] or "暂未明确",
            "products": "、".join(article.get("product_candidates", []))[:500] or "暂未明确",
            "risk_level": article.get("risk_level") or "low",
            "risk_score": int(article.get("risk_score") or 0),
            "finding": str(article.get("summary") or "暂未明确")[:1000],
            "impact_reason": "；".join(risk_factors)[:1000] or "暂未明确",
            "business_impact": str(
                article.get("impact_analysis") or "暂未明确"
            )[:1000],
            "recommended_action": "；".join(actions)[:1000] or "暂未明确",
        }
        if development is None:
            return KeyDevelopment.model_validate(defaults)
        update = {
            "risk_level": defaults["risk_level"],
            "risk_score": defaults["risk_score"],
        }
        for field in (
            "title",
            "affected_region",
            "products",
            "finding",
            "impact_reason",
            "business_impact",
            "recommended_action",
        ):
            if str(getattr(development, field, "") or "").strip() in {"", "暂未明确"}:
                update[field] = defaults[field]
        return development.model_copy(update=update)

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
        if self.repository.has_successful_report(report_date):
            return
        try:
            report_id, articles = self.report_manager.prepare(report_date)
            self.report_manager.execute(report_id, report_date, articles)
        except (IntelligenceAlreadyRunningError, ValueError):
            return
