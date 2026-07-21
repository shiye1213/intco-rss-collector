from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from trafilatura import extract


class ContentFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContentDocument:
    requested_url: str
    final_url: str
    full_text: str
    content_hash: str
    content_chars: int
    http_status: int
    content_type: str
    extractor: str = "trafilatura"


class ArticleContentFetcher(Protocol):
    def fetch(self, url: str) -> ContentDocument: ...


class ArticleURLResolver(Protocol):
    def resolve(self, url: str) -> str: ...


def validate_public_http_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContentFetchError("全文地址必须是有效的 HTTP 或 HTTPS URL")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ContentFetchError("全文地址不能指向本机或内网")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ContentFetchError(f"无法解析全文地址域名: {exc}") from exc
    if not addresses:
        raise ContentFetchError("全文地址域名没有可用 IP")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ContentFetchError("全文地址不能指向本机、内网或保留地址")
    return parsed.geturl()


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp,
        code: int,
        msg: str,
        headers,
        newurl: str,
    ) -> Request | None:
        target = validate_public_http_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, target)


class _GoogleNewsParamsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.signature: str | None = None
        self.timestamp: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        signature = values.get("data-n-a-sg")
        timestamp = values.get("data-n-a-ts")
        if signature and timestamp:
            self.signature = signature
            self.timestamp = timestamp


class GoogleNewsURLResolver:
    """Resolve opaque Google News RSS article links to publisher URLs."""

    _RPC_URL = (
        "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
    )

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        max_download_bytes: int = 2_000_000,
        opener=None,
    ) -> None:
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes
        self._opener = opener or build_opener(_SafeRedirectHandler())

    def resolve(self, url: str) -> str:
        validated_url = validate_public_http_url(url)
        parsed = urlsplit(validated_url)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.hostname != "news.google.com"
            or len(parts) < 2
            or parts[-2] not in {"articles", "read"}
        ):
            return validated_url

        article_id = parts[-1]
        signature, timestamp = self._get_decoding_params(article_id)
        return self._decode_url(article_id, signature, timestamp)

    def _get_decoding_params(self, article_id: str) -> tuple[str, int]:
        params_url = validate_public_http_url(
            f"https://news.google.com/articles/{article_id}"
        )
        request = Request(params_url, headers=_browser_headers())
        html = self._read_response(request, "Google News 解析参数").decode(
            "utf-8", errors="replace"
        )
        parser = _GoogleNewsParamsParser()
        parser.feed(html)
        if not parser.signature or not parser.timestamp:
            raise ContentFetchError("Google News 未返回原文链接解析参数")
        try:
            timestamp = int(parser.timestamp)
        except ValueError as exc:
            raise ContentFetchError("Google News 原文链接解析时间戳无效") from exc
        return parser.signature, timestamp

    def _decode_url(self, article_id: str, signature: str, timestamp: int) -> str:
        rpc_argument = [
            "garturlreq",
            [
                [
                    "X",
                    "X",
                    ["X", "X"],
                    None,
                    None,
                    1,
                    1,
                    "US:en",
                    None,
                    1,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    1,
                ],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            article_id,
            timestamp,
            signature,
        ]
        envelope = [
            [
                [
                    "Fbv4je",
                    json.dumps(rpc_argument, ensure_ascii=False, separators=(",", ":")),
                ]
            ]
        ]
        body = urlencode(
            {
                "f.req": json.dumps(
                    envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            }
        ).encode("utf-8")
        request = Request(
            validate_public_http_url(self._RPC_URL),
            data=body,
            headers={
                **_browser_headers(),
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://news.google.com/",
            },
        )
        response_text = self._read_response(
            request, "Google News 原文链接解析"
        ).decode("utf-8", errors="replace")
        try:
            payload_text = response_text.split("\n\n", 1)[1]
            payload = json.loads(payload_text)
            record = next(
                row
                for row in payload
                if isinstance(row, list)
                and len(row) >= 3
                and row[0] == "wrb.fr"
                and row[1] == "Fbv4je"
                and row[2]
            )
            decoded = json.loads(record[2])
            resolved_url = decoded[1]
        except (IndexError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as exc:
            raise ContentFetchError("Google News 未能解析出出版社原文链接") from exc
        if not isinstance(resolved_url, str):
            raise ContentFetchError("Google News 返回的出版社原文链接无效")
        resolved_url = validate_public_http_url(resolved_url)
        if urlsplit(resolved_url).hostname == "news.google.com":
            raise ContentFetchError("Google News 原文链接仍指向聚合页面")
        return resolved_url

    def _read_response(self, request: Request, label: str) -> bytes:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(self.max_download_bytes + 1)
                if len(body) > self.max_download_bytes:
                    raise ContentFetchError(f"{label}响应超过下载限制")
                return body
        except HTTPError as exc:
            raise ContentFetchError(f"{label}返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ContentFetchError(f"{label}请求失败: {exc}") from exc


def _browser_headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 INTCO-News-Research/1.0"
        ),
    }


class WebContentFetcher:
    def __init__(
        self,
        *,
        timeout: float = 25.0,
        max_download_bytes: int = 5_000_000,
        max_text_chars: int = 250_000,
        min_text_chars: int = 200,
        url_resolver: ArticleURLResolver | None = None,
        opener=None,
    ) -> None:
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes
        self.max_text_chars = max_text_chars
        self.min_text_chars = min_text_chars
        self._opener = opener or build_opener(_SafeRedirectHandler())
        self.url_resolver = url_resolver or GoogleNewsURLResolver(
            timeout=timeout,
            opener=self._opener,
        )

    def fetch(self, url: str) -> ContentDocument:
        requested_url = validate_public_http_url(url)
        download_url = self.url_resolver.resolve(requested_url)
        request = Request(
            download_url,
            headers=_browser_headers(),
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                final_url = validate_public_http_url(response.geturl())
                http_status = int(getattr(response, "status", 200))
                content_type = response.headers.get_content_type().casefold()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ContentFetchError(
                        f"全文页面不是 HTML，Content-Type={content_type}"
                    )
                body = response.read(self.max_download_bytes + 1)
                if len(body) > self.max_download_bytes:
                    raise ContentFetchError("全文页面超过 5 MB 下载限制")
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise ContentFetchError(f"全文页面返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ContentFetchError(f"全文页面请求失败: {exc}") from exc

        try:
            html = body.decode(charset, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
        extracted = extract(
            html,
            url=final_url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            deduplicate=True,
        )
        full_text = self._normalize_text(extracted or "")
        if len(full_text) < self.min_text_chars:
            raise ContentFetchError(
                f"正文抽取结果过短，仅 {len(full_text)} 个字符"
            )
        full_text = full_text[: self.max_text_chars]
        return ContentDocument(
            requested_url=requested_url,
            final_url=final_url,
            full_text=full_text,
            content_hash=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
            content_chars=len(full_text),
            http_status=http_status,
            content_type=content_type,
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        lines: list[str] = []
        previous_blank = False
        for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
            if line:
                lines.append(line)
                previous_blank = False
            elif lines and not previous_blank:
                lines.append("")
                previous_blank = True
        return "\n".join(lines).strip()
