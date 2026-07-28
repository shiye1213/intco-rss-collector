from __future__ import annotations

import hashlib
import html
import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import (
    parse_qsl,
    quote_plus,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .database import Database, utc_now_iso
from .normalization import normalize_categories, normalize_publisher
from .query_builder import build_keyword_query, localize_keyword_for_source


USER_AGENT = "INTCO-RSS-Collector/1.0 (+internal market intelligence)"
TRACKING_PARAMETERS = {"gclid", "fbclid", "mc_cid", "mc_eid"}
MAX_CRAWL_ARTICLES = 30
_ARTICLE_JSON_LD_TYPES = {"article", "blogposting", "newsarticle", "report"}
_ARTICLE_PATH_HINTS = {
    "article",
    "articles",
    "bulletin",
    "news",
    "notice",
    "policy",
    "press",
    "regulation",
    "release",
    "story",
}
_NON_ARTICLE_PATH_HINTS = {
    "about",
    "account",
    "archive",
    "author",
    "category",
    "contact",
    "cookie",
    "events",
    "login",
    "privacy",
    "search",
    "subscribe",
    "tag",
    "terms",
}
_NON_HTML_SUFFIXES = {
    ".avi",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


class CollectionAlreadyRunningError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def value(self) -> str:
        return " ".join(" ".join(self.parts).split())


class _CrawlHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.anchors: list[tuple[str, str]] = []
        self.json_ld_blocks: list[str] = []
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.time_values: list[str] = []
        self.base_url = ""
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._capture_title = False
        self._capture_heading = False
        self._capture_time = False
        self._time_parts: list[str] = []
        self._capture_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        attributes = {
            key.casefold(): (value or "").strip()
            for key, value in attrs
        }
        if tag == "base" and not self.base_url:
            self.base_url = attributes.get("href", "")
        elif tag == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).casefold()
            content = attributes.get("content", "")
            if key and content and key not in self.metadata:
                self.metadata[key] = content
        elif tag == "a" and self._anchor_href is None:
            self._anchor_href = attributes.get("href", "")
            self._anchor_parts = []
        elif tag == "title":
            self._capture_title = True
        elif tag == "h1" and not self.heading_parts:
            self._capture_heading = True
        elif tag == "time":
            value = attributes.get("datetime", "")
            if value:
                self.time_values.append(value)
            self._capture_time = True
            self._time_parts = []
        elif (
            tag == "script"
            and attributes.get("type", "").casefold()
            == "application/ld+json"
        ):
            self._capture_json_ld = True
            self._json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href is not None:
            text = " ".join(" ".join(self._anchor_parts).split())
            if self._anchor_href and text:
                self.anchors.append((self._anchor_href, text))
            self._anchor_href = None
            self._anchor_parts = []
        elif tag == "title":
            self._capture_title = False
        elif tag == "h1":
            self._capture_heading = False
        elif tag == "time":
            value = " ".join(" ".join(self._time_parts).split())
            if value:
                self.time_values.append(value)
            self._capture_time = False
            self._time_parts = []
        elif tag == "script" and self._capture_json_ld:
            value = "".join(self._json_ld_parts).strip()
            if value:
                self.json_ld_blocks.append(value)
            self._capture_json_ld = False
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._capture_title:
            self.title_parts.append(data)
        if self._capture_heading:
            self.heading_parts.append(data)
        if self._capture_time:
            self._time_parts.append(data)
        if self._capture_json_ld:
            self._json_ld_parts.append(data)


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    publisher: str
    summary: str
    published_at: datetime | None
    guid: str = ""
    categories: tuple[str, ...] = ()


def strip_html(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value or ""))
    except Exception:
        return " ".join(html.unescape(value or "").split())
    return parser.value()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_child_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in element:
        if local_name(child.tag) in names:
            value = " ".join("".join(child.itertext()).split())
            if value:
                return value
    return ""


def parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_feed(xml_data: bytes) -> list[FeedItem]:
    root = ElementTree.fromstring(xml_data)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    items: list[FeedItem] = []
    for entry in entries:
        title = strip_html(first_child_text(entry, ("title",)))
        summary = strip_html(
            first_child_text(entry, ("description", "summary", "content", "encoded"))
        )
        publisher = normalize_publisher(
            strip_html(first_child_text(entry, ("source", "author", "creator")))
        )
        guid = strip_html(first_child_text(entry, ("guid", "id")))
        categories = normalize_categories(
            [
                strip_html(child.attrib.get("term") or child.attrib.get("label") or child.text or "")
                for child in entry
                if local_name(child.tag) == "category"
            ]
        )
        link = ""
        for child in entry:
            if local_name(child.tag) != "link":
                continue
            href = child.attrib.get("href", "").strip()
            rel = child.attrib.get("rel", "alternate")
            if href and rel in {"alternate", ""}:
                link = href
                break
            if child.text and not link:
                link = child.text.strip()
        published = parse_datetime(
            first_child_text(entry, ("pubDate", "published", "updated", "date"))
        )
        if title and link:
            items.append(
                FeedItem(
                    title=title,
                    url=html.unescape(link),
                    publisher=publisher,
                    summary=summary,
                    published_at=published,
                    guid=guid,
                    categories=tuple(categories),
                )
            )
    return items


def decode_html_document(data: bytes) -> str:
    prefix = data[:4096]
    match = re.search(
        br"""charset\s*=\s*["']?\s*([a-zA-Z0-9._-]+)""",
        prefix,
        flags=re.IGNORECASE,
    )
    encodings = ["utf-8-sig"]
    if match:
        encodings.insert(0, match.group(1).decode("ascii", errors="ignore"))
    encodings.extend(("gb18030", "latin-1"))
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def looks_like_html(data: bytes) -> bool:
    prefix = data[:4096].lstrip().lower()
    return any(
        marker in prefix
        for marker in (b"<!doctype html", b"<html", b"<article", b"<a ")
    )


def parse_crawl_document(data: bytes) -> _CrawlHTMLParser:
    parser = _CrawlHTMLParser()
    parser.feed(decode_html_document(data))
    parser.close()
    return parser


def _iter_json_objects(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _json_ld_types(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {
            item.casefold()
            for item in value
            if isinstance(item, str)
        }
    return set()


def _json_ld_publisher(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else ""
    return ""


def _json_ld_url(value: dict[str, object], page_url: str) -> str:
    candidate = value.get("url") or value.get("mainEntityOfPage") or ""
    if isinstance(candidate, dict):
        candidate = candidate.get("@id") or candidate.get("url") or ""
    if not isinstance(candidate, str):
        return page_url
    return urljoin(page_url, candidate)


def _json_ld_items(
    parser: _CrawlHTMLParser,
    page_url: str,
    default_publisher: str,
) -> list[FeedItem]:
    items: list[FeedItem] = []
    for block in parser.json_ld_blocks:
        try:
            payload = json.loads(block)
        except (TypeError, ValueError):
            continue
        for value in _iter_json_objects(payload):
            if not (_json_ld_types(value.get("@type")) & _ARTICLE_JSON_LD_TYPES):
                continue
            title = strip_html(
                str(value.get("headline") or value.get("name") or "")
            )
            published_at = parse_datetime(
                str(
                    value.get("datePublished")
                    or value.get("dateCreated")
                    or value.get("dateModified")
                    or ""
                )
            )
            article_url = _json_ld_url(value, page_url)
            if not title or published_at is None or not article_url:
                continue
            section = value.get("articleSection")
            categories = normalize_categories(
                section if isinstance(section, list) else [str(section or "")]
            )
            items.append(
                FeedItem(
                    title=title,
                    url=article_url,
                    publisher=normalize_publisher(
                        _json_ld_publisher(value.get("publisher"))
                        or default_publisher
                    ),
                    summary=strip_html(
                        str(value.get("description") or value.get("abstract") or "")
                    ),
                    published_at=published_at,
                    guid=article_url,
                    categories=tuple(categories),
                )
            )
    return items


def _first_metadata(parser: _CrawlHTMLParser, *keys: str) -> str:
    for key in keys:
        value = parser.metadata.get(key.casefold(), "").strip()
        if value:
            return value
    return ""


def _date_from_url(url: str) -> datetime | None:
    path = urlsplit(url).path
    match = re.search(
        r"/(20\d{2})[/-](0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])(?:/|$)",
        path,
    )
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def extract_crawled_article(
    data: bytes,
    page_url: str,
    default_publisher: str = "",
) -> FeedItem | None:
    parser = parse_crawl_document(data)
    json_ld_items = _json_ld_items(parser, page_url, default_publisher)
    canonical_page_url = canonicalize_url(page_url)
    for item in json_ld_items:
        if canonicalize_url(item.url) == canonical_page_url:
            return item
    if len(json_ld_items) == 1:
        return json_ld_items[0]

    title = strip_html(
        _first_metadata(
            parser,
            "og:title",
            "twitter:title",
            "headline",
        )
        or " ".join(parser.heading_parts)
        or " ".join(parser.title_parts)
    )
    published_at = None
    date_values = [
        _first_metadata(
            parser,
            "article:published_time",
            "datepublished",
            "date",
            "dc.date",
            "dcterms.date",
            "pubdate",
        ),
        *parser.time_values,
    ]
    for value in date_values:
        published_at = parse_datetime(value)
        if published_at is not None:
            break
    published_at = published_at or _date_from_url(page_url)
    if not title or published_at is None:
        return None
    publisher = normalize_publisher(
        _first_metadata(parser, "og:site_name", "author")
        or default_publisher
    )
    summary = strip_html(
        _first_metadata(
            parser,
            "og:description",
            "twitter:description",
            "description",
        )
    )
    categories = normalize_categories(
        [_first_metadata(parser, "article:section", "section")]
    )
    return FeedItem(
        title=title,
        url=page_url,
        publisher=publisher,
        summary=summary,
        published_at=published_at,
        guid=page_url,
        categories=tuple(categories),
    )


def _same_site(left: str, right: str) -> bool:
    left_host = (urlsplit(left).hostname or "").casefold()
    right_host = (urlsplit(right).hostname or "").casefold()
    if left_host.startswith("www."):
        left_host = left_host[4:]
    if right_host.startswith("www."):
        right_host = right_host[4:]
    return bool(left_host and left_host == right_host)


def _crawl_link_score(url: str, text: str) -> int | None:
    parsed = urlsplit(url)
    path = parsed.path.casefold()
    suffix = next(
        (item for item in _NON_HTML_SUFFIXES if path.endswith(item)),
        "",
    )
    if suffix or path in {"", "/"}:
        return None
    segments = {
        segment
        for segment in re.split(r"[/_.-]+", path)
        if segment
    }
    if segments & _NON_ARTICLE_PATH_HINTS:
        return None
    cleaned_text = " ".join(text.split())
    if len(cleaned_text) < 6 or len(cleaned_text) > 300:
        return None
    score = 2
    if segments & _ARTICLE_PATH_HINTS:
        score += 3
    if re.search(r"/20\d{2}[/-]\d{1,2}[/-]\d{1,2}(?:/|$)", path):
        score += 4
    if re.search(r"\d{4,}", path):
        score += 1
    if len([segment for segment in path.split("/") if segment]) >= 2:
        score += 1
    return score


def crawl_web_page(
    page_url: str,
    page_data: bytes,
    fetcher: Callable[[str, float], bytes],
    timeout: float,
    *,
    publisher: str = "",
    max_articles: int = MAX_CRAWL_ARTICLES,
) -> list[FeedItem]:
    parser = parse_crawl_document(page_data)
    proposed_base_url = (
        urljoin(page_url, parser.base_url)
        if parser.base_url
        else page_url
    )
    base_url = (
        proposed_base_url
        if _same_site(page_url, proposed_base_url)
        else page_url
    )
    items: list[FeedItem] = []
    seen_urls: set[str] = set()

    def add_item(item: FeedItem) -> None:
        parsed = urlsplit(item.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not _same_site(page_url, item.url)
        ):
            return
        canonical_url = canonicalize_url(item.url)
        if canonical_url in seen_urls or len(items) >= max_articles:
            return
        seen_urls.add(canonical_url)
        items.append(item)

    for item in _json_ld_items(parser, base_url, publisher):
        add_item(item)

    current_item = extract_crawled_article(page_data, page_url, publisher)
    if current_item is not None:
        add_item(current_item)

    ranked_links: list[tuple[int, int, str]] = []
    candidate_urls: set[str] = set()
    for position, (href, text) in enumerate(parser.anchors):
        url = urljoin(base_url, html.unescape(href))
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not _same_site(base_url, url)
            or canonicalize_url(url) == canonicalize_url(page_url)
        ):
            continue
        canonical_url = canonicalize_url(url)
        if canonical_url in candidate_urls or canonical_url in seen_urls:
            continue
        score = _crawl_link_score(url, text)
        if score is None:
            continue
        candidate_urls.add(canonical_url)
        ranked_links.append((-score, position, url))

    failures: list[str] = []
    for _score, _position, url in sorted(ranked_links)[: max_articles * 2]:
        if len(items) >= max_articles:
            break
        try:
            item = extract_crawled_article(fetcher(url, timeout), url, publisher)
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}")
            continue
        if item is not None:
            add_item(item)

    if not items:
        detail = f"，其中 {len(failures)} 个详情页下载失败" if failures else ""
        raise ValueError(f"网页爬虫未提取到带发布日期的文章{detail}")
    return items


def fetch_feed(url: str, timeout: float = 30) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def build_feed_url(source: dict[str, object], keyword: dict[str, object]) -> str:
    template = str(source["url_template"])
    if source["mode"] == "search":
        query = str(keyword["query"])
        site_domain = str(source.get("site_domain", "")).strip()
        if site_domain:
            site_queries = [
                f"site:{domain.strip()}"
                for domain in site_domain.split(" OR ")
                if domain.strip()
            ]
            site_query = (
                site_queries[0]
                if len(site_queries) == 1
                else f"({' OR '.join(site_queries)})"
            )
            query = f"{site_query} {query}"
        return template.replace("{query}", quote_plus(query))
    return template


def canonicalize_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    filtered_query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    host = (parts.hostname or "").lower()
    try:
        port_number = parts.port
    except ValueError:
        return value.strip()
    port = f":{port_number}" if port_number else ""
    netloc = f"{host}{port}"
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path.rstrip("/") or "/", urlencode(filtered_query), "")
    )


def match_terms(item: FeedItem, terms: list[str]) -> list[str]:
    text = f"{item.title}\n{item.summary}".casefold()
    return [term for term in terms if term.strip() and term.strip().casefold() in text]


def article_fingerprint(item: FeedItem) -> str:
    published_date = item.published_at.date().isoformat() if item.published_at else ""
    payload = "|".join(
        (
            " ".join(item.title.casefold().split()),
            normalize_publisher(item.publisher).casefold(),
            published_date,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def start_of_local_day(now: datetime, timezone_name: str) -> datetime:
    return start_of_local_lookback(now, timezone_name, 1)


def start_of_local_lookback(
    now: datetime, timezone_name: str, lookback_days: int
) -> datetime:
    days = max(1, min(365, int(lookback_days)))
    local = now.astimezone(ZoneInfo(timezone_name)) - timedelta(days=days - 1)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


class Collector:
    def __init__(
        self,
        database: Database,
        feed_fetcher: Callable[[str, float], bytes] = fetch_feed,
        timeout: float = 30,
    ) -> None:
        self.database = database
        self.feed_fetcher = feed_fetcher
        self.timeout = timeout

    def _load_source_items(
        self,
        source: dict[str, object],
        url: str,
    ) -> list[FeedItem]:
        data = self.feed_fetcher(url, self.timeout)
        if source["mode"] == "crawler":
            return crawl_web_page(
                url,
                data,
                self.feed_fetcher,
                self.timeout,
                publisher=str(source.get("name", "")),
            )
        try:
            items = parse_feed(data)
        except ElementTree.ParseError:
            if not looks_like_html(data):
                raise
            return crawl_web_page(
                url,
                data,
                self.feed_fetcher,
                self.timeout,
                publisher=str(source.get("name", "")),
            )
        if not items and looks_like_html(data):
            return crawl_web_page(
                url,
                data,
                self.feed_fetcher,
                self.timeout,
                publisher=str(source.get("name", "")),
            )
        return items

    def collect(self, run_id: int, run_started_at: datetime) -> None:
        sources = self.database.get_sources(active_only=True)
        keywords = self.database.get_keywords(active_only=True)
        settings = self.database.get_settings()
        timezone_name = settings.get("timezone", "Asia/Shanghai")
        incremental_collection = (
            settings.get("incremental_collection", "true") == "true"
        )
        search_local_keyword_filter = (
            settings.get("search_local_keyword_filter", "true") == "true"
        )
        end_iso = iso_utc(run_started_at)

        if not sources or not keywords:
            self.database.fail_run(run_id, "没有启用的数据源或关键词")
            return

        routed_sources: list[tuple[dict[str, object], list[dict[str, object]]]] = []
        for source in sources:
            source_keywords = [
                routed
                for keyword in keywords
                if (
                    routed := localize_keyword_for_source(
                        keyword, str(source.get("language", ""))
                    )
                )
                is not None
            ]
            if source_keywords:
                routed_sources.append((source, source_keywords))

        if not routed_sources:
            self.database.fail_run(run_id, "没有语言匹配的数据源与关键词组合")
            return

        totals = {
            "tasks_total": sum(
                len(source_keywords)
                for _source, source_keywords in routed_sources
            ),
            "tasks_succeeded": 0,
            "tasks_failed": 0,
            "items_seen": 0,
            "items_matched": 0,
            "items_inserted": 0,
            "duplicates": 0,
        }

        with self.database.connect() as connection:
            for source, source_keywords in routed_sources:
                if source["mode"] in {"direct", "crawler"}:
                    url = build_feed_url(source, source_keywords[0])
                    try:
                        feed_items = self._load_source_items(source, url)
                        totals["items_seen"] += len(feed_items)
                    except Exception as exc:
                        for keyword in source_keywords:
                            first_window = start_of_local_lookback(
                                run_started_at,
                                timezone_name,
                                int(keyword.get("lookback_days", 1)),
                            )
                            window_start = self._window_start(
                                connection,
                                source["id"],
                                keyword["id"],
                                first_window,
                                use_cursor=incremental_collection,
                            )
                            self._insert_detail(
                                connection,
                                run_id,
                                source,
                                keyword,
                                window_start,
                                run_started_at,
                                url,
                                status="failed",
                                error_message=f"{type(exc).__name__}: {exc}",
                            )
                            totals["tasks_failed"] += 1
                        connection.commit()
                        continue

                    for keyword in source_keywords:
                        first_window = start_of_local_lookback(
                            run_started_at,
                            timezone_name,
                            int(keyword.get("lookback_days", 1)),
                        )
                        self._process_pair(
                            connection,
                            run_id,
                            source,
                            keyword,
                            url,
                            feed_items,
                            first_window,
                            run_started_at,
                            totals,
                            seen_count=len(feed_items),
                            use_cursor=incremental_collection,
                        )
                        connection.commit()
                else:
                    for keyword in source_keywords:
                        first_window = start_of_local_lookback(
                            run_started_at,
                            timezone_name,
                            int(keyword.get("lookback_days", 1)),
                        )
                        window_start = self._window_start(
                            connection,
                            source["id"],
                            keyword["id"],
                            first_window,
                            use_cursor=incremental_collection,
                        )
                        runtime_keyword = self._runtime_search_keyword(
                            keyword, window_start, run_started_at
                        )
                        url = build_feed_url(source, runtime_keyword)
                        try:
                            feed_items = self._load_source_items(source, url)
                            totals["items_seen"] += len(feed_items)
                            self._process_pair(
                                connection,
                                run_id,
                                source,
                                keyword,
                                url,
                                feed_items,
                                first_window,
                                run_started_at,
                                totals,
                                seen_count=len(feed_items),
                                use_cursor=incremental_collection,
                                apply_local_keyword_filter=(
                                    search_local_keyword_filter
                                    or bool(keyword.get("require_local_match", False))
                                ),
                            )
                        except Exception as exc:
                            self._insert_detail(
                                connection,
                                run_id,
                                source,
                                keyword,
                                window_start,
                                run_started_at,
                                url,
                                status="failed",
                                error_message=f"{type(exc).__name__}: {exc}",
                            )
                            totals["tasks_failed"] += 1
                        connection.commit()

            status = "success"
            message = "采集完成"
            if totals["tasks_failed"] and totals["tasks_succeeded"]:
                status = "partial"
                message = "部分采集任务失败，失败任务的游标未推进"
            elif totals["tasks_failed"] and not totals["tasks_succeeded"]:
                status = "failed"
                message = "所有采集任务均失败"
            connection.execute(
                """
                UPDATE collection_runs
                SET status = ?, finished_at = ?, window_end = ?,
                    tasks_total = ?, tasks_succeeded = ?, tasks_failed = ?,
                    items_seen = ?, items_matched = ?, items_inserted = ?,
                    duplicates = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now_iso(),
                    end_iso,
                    totals["tasks_total"],
                    totals["tasks_succeeded"],
                    totals["tasks_failed"],
                    totals["items_seen"],
                    totals["items_matched"],
                    totals["items_inserted"],
                    totals["duplicates"],
                    message,
                    run_id,
                ),
            )

    @staticmethod
    def _runtime_search_keyword(
        keyword: dict[str, object],
        window_start: datetime,
        run_started_at: datetime,
    ) -> dict[str, object]:
        elapsed_days = max(
            1,
            int((run_started_at - window_start).total_seconds() // 86400) + 1,
        )
        configured_days = int(keyword.get("lookback_days", 1))
        lookback_days = min(365, max(configured_days, elapsed_days))
        return {
            **keyword,
            "query": build_keyword_query(
                list(keyword["match_terms"]),
                context_terms=list(keyword.get("context_terms", [])),
                exclude_terms=list(keyword.get("exclude_terms", [])),
                lookback_days=lookback_days,
            ),
        }

    @staticmethod
    def _window_start(
        connection: Any,
        source_id: int,
        keyword_id: int,
        first_window: datetime,
        *,
        use_cursor: bool = True,
    ) -> datetime:
        if not use_cursor:
            return first_window
        row = connection.execute(
            """
            SELECT last_collected_at FROM collection_cursors
            WHERE rss_source_id = ? AND keyword_id = ?
            """,
            (source_id, keyword_id),
        ).fetchone()
        if row is None:
            return first_window
        parsed = parse_datetime(row["last_collected_at"])
        return parsed or first_window

    def _process_pair(
        self,
        connection: Any,
        run_id: int,
        source: dict[str, object],
        keyword: dict[str, object],
        url: str,
        feed_items: list[FeedItem],
        first_window: datetime,
        run_started_at: datetime,
        totals: dict[str, int],
        *,
        seen_count: int,
        use_cursor: bool = True,
        apply_local_keyword_filter: bool = True,
    ) -> None:
        window_start = self._window_start(
            connection,
            int(source["id"]),
            int(keyword["id"]),
            first_window,
            use_cursor=use_cursor,
        )
        matched_count = 0
        inserted_count = 0
        duplicate_count = 0
        skipped_outside_window = 0
        skipped_without_date = 0
        terms = list(keyword["match_terms"])

        for item in feed_items:
            if item.published_at is None:
                skipped_without_date += 1
                continue
            if not (window_start <= item.published_at <= run_started_at):
                skipped_outside_window += 1
                continue
            matched = match_terms(item, terms)
            if apply_local_keyword_filter and not matched:
                continue
            matched_count += 1
            article_id, inserted = self._upsert_article(
                connection, item, source, url, run_started_at
            )
            connection.execute(
                """
                INSERT INTO article_keywords (article_id, keyword_id, matched_terms)
                VALUES (?, ?, ?)
                ON CONFLICT(article_id, keyword_id) DO UPDATE
                SET matched_terms = excluded.matched_terms
                """,
                (article_id, keyword["id"], json.dumps(matched, ensure_ascii=False)),
            )
            if inserted:
                inserted_count += 1
            else:
                duplicate_count += 1

        connection.execute(
            """
            INSERT INTO collection_cursors (rss_source_id, keyword_id, last_collected_at)
            VALUES (?, ?, ?)
            ON CONFLICT(rss_source_id, keyword_id)
            DO UPDATE SET last_collected_at = excluded.last_collected_at
            """,
            (source["id"], keyword["id"], iso_utc(run_started_at)),
        )
        self._insert_detail(
            connection,
            run_id,
            source,
            keyword,
            window_start,
            run_started_at,
            url,
            status="success",
            items_seen=seen_count,
            items_matched=matched_count,
            items_inserted=inserted_count,
            duplicates=duplicate_count,
            skipped_outside_window=skipped_outside_window,
            skipped_without_date=skipped_without_date,
        )
        totals["tasks_succeeded"] += 1
        totals["items_matched"] += matched_count
        totals["items_inserted"] += inserted_count
        totals["duplicates"] += duplicate_count

    @staticmethod
    def _upsert_article(
        connection: Any,
        item: FeedItem,
        source: dict[str, object],
        feed_url: str,
        collected_at: datetime,
    ) -> tuple[int, bool]:
        canonical_url = canonicalize_url(item.url)
        fingerprint = article_fingerprint(item)
        publisher_normalized = normalize_publisher(item.publisher)
        existing = connection.execute(
            """
            SELECT id FROM articles
            WHERE fingerprint = ? OR canonical_url = ?
            LIMIT 1
            """,
            (fingerprint, canonical_url),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE articles
                SET summary = CASE WHEN length(?) > length(summary) THEN ? ELSE summary END,
                    publisher = CASE WHEN publisher = '' THEN ? ELSE publisher END,
                    publisher_normalized = CASE
                        WHEN publisher_normalized = '' THEN ? ELSE publisher_normalized
                    END
                WHERE id = ?
                """,
                (
                    item.summary,
                    item.summary,
                    item.publisher,
                    publisher_normalized,
                    existing["id"],
                ),
            )
            article_id = int(existing["id"])
            inserted = False
        else:
            cursor = connection.execute(
                """
                INSERT INTO articles
                    (title, url, canonical_url, fingerprint, publisher,
                     publisher_normalized, summary, published_at, collected_at,
                     rss_source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.title,
                    item.url,
                    canonical_url,
                    fingerprint,
                    item.publisher,
                    publisher_normalized,
                    item.summary,
                    iso_utc(item.published_at),
                    iso_utc(collected_at),
                    source["id"],
                ),
            )
            article_id = int(cursor.lastrowid)
            inserted = True

        seen_at = iso_utc(collected_at)
        connection.execute(
            """
            INSERT INTO article_sources
                (article_id, rss_source_id, feed_url, observed_url,
                 canonical_url, guid, language, country, categories,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id, rss_source_id, canonical_url) DO UPDATE SET
                feed_url = excluded.feed_url,
                observed_url = excluded.observed_url,
                guid = CASE WHEN excluded.guid <> '' THEN excluded.guid ELSE guid END,
                language = CASE
                    WHEN excluded.language <> '' THEN excluded.language ELSE language
                END,
                country = CASE
                    WHEN excluded.country <> '' THEN excluded.country ELSE country
                END,
                categories = CASE
                    WHEN excluded.categories <> '[]' THEN excluded.categories ELSE categories
                END,
                last_seen_at = excluded.last_seen_at
            """,
            (
                article_id,
                source["id"],
                feed_url,
                item.url,
                canonical_url,
                item.guid,
                source.get("language", ""),
                source.get("country", ""),
                json.dumps(list(item.categories), ensure_ascii=False),
                seen_at,
                seen_at,
            ),
        )
        return article_id, inserted

    @staticmethod
    def _insert_detail(
        connection: Any,
        run_id: int,
        source: dict[str, object],
        keyword: dict[str, object],
        window_start: datetime,
        window_end: datetime,
        feed_url: str,
        *,
        status: str,
        items_seen: int = 0,
        items_matched: int = 0,
        items_inserted: int = 0,
        duplicates: int = 0,
        skipped_outside_window: int = 0,
        skipped_without_date: int = 0,
        error_message: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO collection_run_details
                (run_id, rss_source_id, keyword_id, status, window_start,
                 window_end, feed_url, items_seen, items_matched, items_inserted,
                 duplicates, skipped_outside_window, skipped_without_date, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source["id"],
                keyword["id"],
                status,
                iso_utc(window_start),
                iso_utc(window_end),
                feed_url,
                items_seen,
                items_matched,
                items_inserted,
                duplicates,
                skipped_outside_window,
                skipped_without_date,
                error_message[:2000],
            ),
        )


class CollectionManager:
    def __init__(self, database: Database, collector: Collector) -> None:
        self.database = database
        self.collector = collector
        self.on_complete: Callable[[], None] | None = None
        self._state_lock = threading.Lock()
        self._running_run_id: int | None = None

    @property
    def running_run_id(self) -> int | None:
        with self._state_lock:
            return self._running_run_id

    def prepare(self, trigger_type: str) -> tuple[int, datetime]:
        with self._state_lock:
            if self._running_run_id is not None:
                raise CollectionAlreadyRunningError(
                    f"采集任务 #{self._running_run_id} 正在运行"
                )
            started_at = datetime.now(UTC)
            settings = self.database.get_settings()
            timezone_name = settings.get("timezone", "Asia/Shanghai")
            incremental_collection = (
                settings.get("incremental_collection", "true") == "true"
            )
            first_window = start_of_local_day(started_at, timezone_name)
            active_sources = self.database.get_sources(active_only=True)
            active_keywords = self.database.get_keywords(active_only=True)
            cursor_values: list[datetime] = []
            if active_sources and active_keywords:
                with self.database.connect() as connection:
                    rows = connection.execute(
                        """
                        SELECT c.last_collected_at, k.lookback_days
                        FROM rss_sources s
                        CROSS JOIN keywords k
                        LEFT JOIN collection_cursors c
                          ON c.rss_source_id = s.id AND c.keyword_id = k.id
                        WHERE s.active = 1 AND s.archived = 0
                          AND k.active = 1 AND k.archived = 0
                        """
                    ).fetchall()
                    cursor_values = [
                        (
                            parse_datetime(row["last_collected_at"])
                            if incremental_collection
                            else None
                        )
                        or start_of_local_lookback(
                            started_at,
                            timezone_name,
                            int(row["lookback_days"]),
                        )
                        for row in rows
                    ]
            global_start = min(cursor_values, default=first_window)
            run_id = self.database.create_run(
                trigger_type, iso_utc(started_at), iso_utc(global_start)
            )
            self._running_run_id = run_id
            return run_id, started_at

    def execute(self, run_id: int, started_at: datetime) -> None:
        try:
            self.collector.collect(run_id, started_at)
        except Exception as exc:
            self.database.fail_run(run_id, f"{type(exc).__name__}: {exc}")
        finally:
            with self._state_lock:
                if self._running_run_id == run_id:
                    self._running_run_id = None
            if self.on_complete is not None:
                try:
                    self.on_complete()
                except Exception:
                    # AI follow-up failure must not alter the completed collection run.
                    pass
