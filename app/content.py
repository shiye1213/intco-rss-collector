from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit


class ContentFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = "unknown",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.retryable = retryable


def http_fetch_error(label: str, status: int) -> ContentFetchError:
    retryable = status in {403, 408, 425, 429} or status >= 500
    return ContentFetchError(
        f"{label}返回 HTTP {status}",
        failure_kind=f"http_{status}",
        retryable=retryable,
    )


@dataclass(frozen=True)
class ArticleReference:
    title: str
    publisher: str
    urls: tuple[str, ...]


@dataclass(frozen=True)
class ContentDocument:
    requested_url: str
    final_url: str
    full_text: str
    content_hash: str
    content_chars: int
    http_status: int
    content_type: str
    extractor: str = "openai-web-search"


class ArticleContentReader(Protocol):
    model: str

    @property
    def configured(self) -> bool: ...

    def read(self, article: ArticleReference) -> ContentDocument: ...


def validate_public_http_url(url: str) -> str:
    """Reject local/private targets without downloading or resolving the URL."""

    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContentFetchError(
            "全文地址必须是有效的 HTTP 或 HTTPS URL",
            failure_kind="invalid_url",
            retryable=False,
        )
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ContentFetchError(
            "全文地址不能指向本机或内网",
            failure_kind="unsafe_url",
            retryable=False,
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ContentFetchError(
            "全文地址不能指向本机、内网或保留地址",
            failure_kind="unsafe_url",
            retryable=False,
        )
    return parsed.geturl()
