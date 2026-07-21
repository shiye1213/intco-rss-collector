from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, urlsplit


COUNTRY_BY_HOST_SUFFIX = {
    "gov.uk": "GB",
    "gov.br": "BR",
    "canada.ca": "CA",
    "europa.eu": "EU",
    "wto.org": "GLOBAL",
}


def normalize_publisher(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return " ".join(normalized.split()).strip(" -|·")


def normalize_categories(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(unicodedata.normalize("NFKC", value or "").split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def infer_country(url: str, language: str = "") -> str:
    try:
        parts = urlsplit(url.replace("{query}", "keyword"))
        query = parse_qs(parts.query)
    except ValueError:
        parts = urlsplit("")
        query = {}

    google_country = next(iter(query.get("gl", [])), "").strip().upper()
    if google_country:
        return google_country

    locale_match = re.search(r"[-_]([A-Za-z]{2})$", language.strip())
    if locale_match:
        return locale_match.group(1).upper()

    host = (parts.hostname or "").lower()
    for suffix, country in COUNTRY_BY_HOST_SUFFIX.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return country
    return ""
