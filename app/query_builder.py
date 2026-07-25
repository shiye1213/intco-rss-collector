from __future__ import annotations

import re


MAX_GOOGLE_NEWS_QUERY_CHARS = 200
_HAN_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_CHARACTER = re.compile(r"[A-Za-z]")


def _normalize_terms(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(value.replace('"', " ").split())
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            normalized.append(term)
    return normalized


def _term_group(values: list[str]) -> str:
    return "(" + "OR".join(f'"{term}"' for term in values) + ")"


def build_keyword_query(
    match_terms: list[str],
    *,
    context_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    lookback_days: int | None = None,
) -> str:
    """Build a focused Google News query from one keyword strategy.

    Subject terms form the required first group. Context terms form an optional
    second required group, exclusions remove known ambiguity, and recency keeps
    the RSS result window from being dominated by old high-ranking stories.
    """
    normalized = _normalize_terms(match_terms)
    if not normalized:
        raise ValueError("至少需要一个正文匹配词")

    parts = [_term_group(normalized)]
    normalized_context = _normalize_terms(context_terms or [])
    if normalized_context:
        parts.append("AND" + _term_group(normalized_context))
    parts.extend(f'-"{term}"' for term in _normalize_terms(exclude_terms or []))
    if lookback_days is not None:
        if isinstance(lookback_days, bool) or not 1 <= lookback_days <= 365:
            raise ValueError("Google News 回溯天数必须在 1 到 365 之间")
        parts.append(f"when:{lookback_days}d")
    query = "".join(parts)
    if len(query) > MAX_GOOGLE_NEWS_QUERY_CHARS:
        raise ValueError(
            "Google News 查询过长，请拆分为更聚焦的关键词组"
            f"（当前 {len(query)} 字符，上限 {MAX_GOOGLE_NEWS_QUERY_CHARS}）"
        )
    return query


def localize_keyword_for_source(
    keyword: dict[str, object],
    source_language: str,
) -> dict[str, object] | None:
    """Return the source-language slice of one keyword strategy.

    Chinese and English sources receive only terms written for their language.
    A source without language metadata remains backward compatible, while a
    source explicitly marked as another language is skipped until matching
    terms for that language can be represented.
    """
    language = source_language.strip().casefold().split("-", 1)[0]
    if not language:
        return dict(keyword)
    if language not in {"zh", "en"}:
        return None

    match_terms = _terms_for_language(keyword.get("match_terms", []), language)
    if not match_terms:
        return None
    context_terms = _terms_for_language(
        keyword.get("context_terms", []), language
    )
    exclude_terms = _terms_for_language(
        keyword.get("exclude_terms", []), language
    )
    lookback_days = int(keyword.get("lookback_days", 30))
    return {
        **keyword,
        "match_terms": match_terms,
        "context_terms": context_terms,
        "exclude_terms": exclude_terms,
        "query": build_keyword_query(
            match_terms,
            context_terms=context_terms,
            exclude_terms=exclude_terms,
            lookback_days=lookback_days,
        ),
    }


def _terms_for_language(values: object, language: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    selected: list[str] = []
    for value in values:
        term = str(value).strip()
        term_language = _term_language(term)
        if term and (term_language == language or term_language is None):
            selected.append(term)
    return selected


def _term_language(term: str) -> str | None:
    if _HAN_CHARACTER.search(term):
        return "zh"
    if _LATIN_CHARACTER.search(term):
        return "en"
    return None
