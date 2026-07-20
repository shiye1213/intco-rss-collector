from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .database import Database, utc_now_iso


USER_AGENT = "INTCO-RSS-Collector/1.0 (+internal market intelligence)"
TRACKING_PARAMETERS = {"gclid", "fbclid", "mc_cid", "mc_eid"}


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


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    publisher: str
    summary: str
    published_at: datetime | None


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
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
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
            first_child_text(entry, ("description", "summary", "content"))
        )
        publisher = strip_html(first_child_text(entry, ("source", "author")))
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
                )
            )
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
        return template.replace("{query}", quote_plus(str(keyword["query"])))
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
            " ".join(item.publisher.casefold().split()),
            published_date,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def start_of_local_day(now: datetime, timezone_name: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone_name))
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

    def collect(self, run_id: int, run_started_at: datetime) -> None:
        sources = self.database.get_sources(active_only=True)
        keywords = self.database.get_keywords(active_only=True)
        settings = self.database.get_settings()
        timezone_name = settings.get("timezone", "Asia/Shanghai")
        first_window = start_of_local_day(run_started_at, timezone_name)
        end_iso = iso_utc(run_started_at)

        if not sources or not keywords:
            self.database.fail_run(run_id, "没有启用的 RSS 源或关键词")
            return

        totals = {
            "tasks_total": len(sources) * len(keywords),
            "tasks_succeeded": 0,
            "tasks_failed": 0,
            "items_seen": 0,
            "items_matched": 0,
            "items_inserted": 0,
            "duplicates": 0,
        }

        with self.database.connect() as connection:
            for source in sources:
                if source["mode"] == "direct":
                    url = build_feed_url(source, keywords[0])
                    try:
                        feed_items = parse_feed(self.feed_fetcher(url, self.timeout))
                        totals["items_seen"] += len(feed_items)
                    except Exception as exc:
                        for keyword in keywords:
                            window_start = self._window_start(
                                connection, source["id"], keyword["id"], first_window
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

                    for keyword in keywords:
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
                        )
                        connection.commit()
                else:
                    for keyword in keywords:
                        url = build_feed_url(source, keyword)
                        window_start = self._window_start(
                            connection, source["id"], keyword["id"], first_window
                        )
                        try:
                            feed_items = parse_feed(self.feed_fetcher(url, self.timeout))
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
                message = "部分 RSS 任务失败，失败任务的游标未推进"
            elif totals["tasks_failed"] and not totals["tasks_succeeded"]:
                status = "failed"
                message = "所有 RSS 任务均失败"
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
    def _window_start(
        connection: sqlite3.Connection,
        source_id: int,
        keyword_id: int,
        first_window: datetime,
    ) -> datetime:
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
        connection: sqlite3.Connection,
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
    ) -> None:
        window_start = self._window_start(
            connection, int(source["id"]), int(keyword["id"]), first_window
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
            if not matched:
                continue
            matched_count += 1
            article_id, inserted = self._upsert_article(
                connection, item, int(source["id"]), run_started_at
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
        connection: sqlite3.Connection,
        item: FeedItem,
        source_id: int,
        collected_at: datetime,
    ) -> tuple[int, bool]:
        canonical_url = canonicalize_url(item.url)
        fingerprint = article_fingerprint(item)
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
                    publisher = CASE WHEN publisher = '' THEN ? ELSE publisher END
                WHERE id = ?
                """,
                (item.summary, item.summary, item.publisher, existing["id"]),
            )
            return int(existing["id"]), False
        cursor = connection.execute(
            """
            INSERT INTO articles
                (title, url, canonical_url, fingerprint, publisher, summary,
                 published_at, collected_at, rss_source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.title,
                item.url,
                canonical_url,
                fingerprint,
                item.publisher,
                item.summary,
                iso_utc(item.published_at),
                iso_utc(collected_at),
                source_id,
            ),
        )
        return int(cursor.lastrowid), True

    @staticmethod
    def _insert_detail(
        connection: sqlite3.Connection,
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
            first_window = start_of_local_day(
                started_at, settings.get("timezone", "Asia/Shanghai")
            )
            active_sources = self.database.get_sources(active_only=True)
            active_keywords = self.database.get_keywords(active_only=True)
            cursor_values: list[datetime] = []
            if active_sources and active_keywords:
                with self.database.connect() as connection:
                    rows = connection.execute(
                        """
                        SELECT c.last_collected_at
                        FROM rss_sources s
                        CROSS JOIN keywords k
                        LEFT JOIN collection_cursors c
                          ON c.rss_source_id = s.id AND c.keyword_id = k.id
                        WHERE s.active = 1 AND s.archived = 0
                          AND k.active = 1 AND k.archived = 0
                        """
                    ).fetchall()
                    cursor_values = [
                        parse_datetime(row["last_collected_at"]) or first_window
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
