from __future__ import annotations


MAX_GOOGLE_NEWS_QUERY_CHARS = 200


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
    return "(" + " OR ".join(f'"{term}"' for term in values) + ")"


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
        parts.append("AND " + _term_group(normalized_context))
    parts.extend(f'-"{term}"' for term in _normalize_terms(exclude_terms or []))
    if lookback_days is not None:
        if isinstance(lookback_days, bool) or not 1 <= lookback_days <= 365:
            raise ValueError("Google News 回溯天数必须在 1 到 365 之间")
        parts.append(f"when:{lookback_days}d")
    query = " ".join(parts)
    if len(query) > MAX_GOOGLE_NEWS_QUERY_CHARS:
        raise ValueError(
            "Google News 查询过长，请拆分为更聚焦的关键词组"
            f"（当前 {len(query)} 字符，上限 {MAX_GOOGLE_NEWS_QUERY_CHARS}）"
        )
    return query
