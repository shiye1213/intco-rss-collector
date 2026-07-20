from __future__ import annotations


def build_keyword_query(match_terms: list[str]) -> str:
    """Build a Google News query from plain keyword phrases."""
    normalized: list[str] = []
    for value in match_terms:
        term = " ".join(value.replace('"', " ").split())
        if term and term.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(term)
    if not normalized:
        raise ValueError("至少需要一个正文匹配词")
    return "(" + " OR ".join(f'\"{term}\"' for term in normalized) + ")"

