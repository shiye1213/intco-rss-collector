from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .content import (
    ArticleReference,
    ContentDocument,
    ContentFetchError,
    validate_public_http_url,
)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    raw_content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class JSONLLMClient(Protocol):
    model: str

    @property
    def configured(self) -> bool: ...

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
    ) -> LLMResult: ...


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._api_key = (
            api_key
            if api_key is not None
            else os.getenv("DEEPSEEK_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url
            if base_url is not None
            else os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        ).strip().rstrip("/")
        self.model = (
            model
            if model is not None
            else os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        ).strip()
        timeout_value = (
            timeout
            if timeout is not None
            else os.getenv("DEEPSEEK_TIMEOUT", "90")
        )
        try:
            self.timeout = float(timeout_value)
        except (TypeError, ValueError):
            self.timeout = 90.0

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.base_url and self.model)

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
    ) -> LLMResult:
        if not self.configured:
            raise LLMError("尚未配置 DEEPSEEK_API_KEY")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "INTCO-RSS-Collector/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise LLMError(f"DeepSeek API 返回 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"DeepSeek API 连接失败: {exc}") from exc

        try:
            body = json.loads(response_body)
            choice = body["choices"][0]
            raw_content = choice["message"]["content"]
            if choice.get("finish_reason") == "length":
                raise LLMError("DeepSeek 输出超过 max_tokens，JSON 可能不完整")
            if not isinstance(raw_content, str) or not raw_content.strip():
                raise LLMError("DeepSeek 返回了空内容")
            content = raw_content.strip()
            if content.startswith("```"):
                content = content.removeprefix("```json").removeprefix("```")
                content = content.removesuffix("```").strip()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise LLMError("DeepSeek JSON 顶层必须是对象")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"无法解析 DeepSeek JSON 响应: {exc}") from exc

        usage = body.get("usage") or {}
        return LLMResult(
            data=data,
            raw_content=raw_content,
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


class OpenAIWebContentReader:
    """Read an article through the Responses API hosted web-search tool."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_output_tokens: int | None = None,
        opener=None,
    ) -> None:
        self._api_key = (
            api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url
            if base_url is not None
            else os.getenv("OPENAI_BASE_URL", "https://www.cctq.ai/v1")
        ).strip().rstrip("/")
        self.model = (
            model
            if model is not None
            else os.getenv("OPENAI_WEB_MODEL", "gpt-5.4-mini")
        ).strip()
        timeout_value = (
            timeout
            if timeout is not None
            else os.getenv("OPENAI_WEB_TIMEOUT", "180")
        )
        max_tokens_value = (
            max_output_tokens
            if max_output_tokens is not None
            else os.getenv("OPENAI_WEB_MAX_OUTPUT_TOKENS", "32000")
        )
        try:
            self.timeout = float(timeout_value)
        except (TypeError, ValueError):
            self.timeout = 180.0
        try:
            self.max_output_tokens = max(
                2_000, min(128_000, int(max_tokens_value))
            )
        except (TypeError, ValueError):
            self.max_output_tokens = 32_000
        self._open = opener or urlopen

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.base_url and self.model)

    def read(self, article: ArticleReference) -> ContentDocument:
        if not self.configured:
            raise ContentFetchError(
                "尚未配置 OPENAI_API_KEY",
                failure_kind="openai_not_configured",
                retryable=False,
            )
        urls = tuple(
            dict.fromkeys(validate_public_http_url(url) for url in article.urls if url)
        )
        if not urls:
            raise ContentFetchError(
                "没有可供 GPT 网页搜索读取的文章链接",
                failure_kind="no_url",
                retryable=False,
            )

        request = Request(
            self._responses_url(),
            data=json.dumps(
                self._request_payload(article, urls), ensure_ascii=False
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "INTCO-RSS-Collector/1.0",
            },
            method="POST",
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ContentFetchError(
                f"CCTQ/OpenAI 网页读取返回 HTTP {exc.code}: {detail}",
                failure_kind=f"openai_http_{exc.code}",
                retryable=exc.code in {408, 425, 429} or exc.code >= 500,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ContentFetchError(
                f"CCTQ/OpenAI 网页读取连接失败: {exc}",
                failure_kind="openai_network",
                retryable=True,
            ) from exc
        return self._parse_response(response_body, requested_url=urls[0])

    def _responses_url(self) -> str:
        if self.base_url.endswith("/responses"):
            return self.base_url
        return f"{self.base_url}/responses"

    def _request_payload(
        self,
        article: ArticleReference,
        urls: tuple[str, ...],
    ) -> dict[str, Any]:
        task = {
            "article": {
                "title": article.title,
                "publisher": article.publisher,
                "published_at": article.published_at,
                "candidate_urls": list(urls),
            },
            "required_output": {
                "success": "boolean",
                "final_url": "string or null",
                "full_text": "complete article body or null",
                "failure_reason": "string",
            },
        }
        instructions = (
            "You are a news article reader. You must use web search and open the "
            "given candidate URL or the publisher's matching canonical page. "
            "If a candidate is a Google News RSS or redirect URL without an article "
            "body, search for the exact title, publisher, and publication date, then "
            "open the matching canonical publisher page. Continue past similar but "
            "different stories and never substitute another article. "
            "Return only information actually read from that page. Never reconstruct "
            "an article from its title, snippet, or background knowledge. Remove "
            "navigation, ads, related-story lists, and footer text. Set success=true "
            "only when the main article body is available and substantially complete. "
            "Return exactly one JSON object with no Markdown fences. Preserve the "
            "article's original language and paragraph order in full_text."
        )
        return {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "instructions": instructions,
            "input": json.dumps(task, ensure_ascii=False),
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }

    def _parse_response(
        self,
        response_body: str,
        *,
        requested_url: str,
    ) -> ContentDocument:
        try:
            body = json.loads(response_body)
            output = body["output"]
            if not isinstance(output, list):
                raise TypeError("output 不是数组")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ContentFetchError(
                f"无法解析 CCTQ/OpenAI 网页读取响应: {exc}",
                failure_kind="openai_invalid_response",
                retryable=True,
            ) from exc

        if body.get("status") == "incomplete":
            detail = body.get("incomplete_details") or {}
            raise ContentFetchError(
                f"CCTQ/OpenAI 网页读取响应不完整: {detail}",
                failure_kind="openai_incomplete_response",
                retryable=True,
            )

        search_calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "web_search_call"
        ]
        completed_search = any(
            item.get("status") == "completed" for item in search_calls
        )
        if search_calls and not completed_search:
            raise ContentFetchError(
                "CCTQ/OpenAI 网页搜索调用未成功完成",
                failure_kind="openai_web_search_unavailable",
                retryable=True,
            )
        if not completed_search:
            raise ContentFetchError(
                "CCTQ/OpenAI 未提供实际网页搜索记录，拒绝保存推测正文",
                failure_kind="openai_no_web_evidence",
                retryable=True,
            )

        text_blocks = [
            block
            for item in output
            if isinstance(item, dict) and item.get("type") == "message"
            for block in item.get("content", [])
            if isinstance(block, dict)
            and block.get("type") == "output_text"
            and str(block.get("text") or "").strip()
        ]
        raw_text = str(text_blocks[-1].get("text") or "") if text_blocks else ""
        try:
            content = raw_text.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else ""
                if content.endswith("```"):
                    content = content[:-3]
            result = json.loads(content.strip())
            if not isinstance(result, dict):
                raise TypeError("JSON 顶层不是对象")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContentFetchError(
                f"CCTQ/OpenAI 网页读取未返回有效 JSON: {exc}",
                failure_kind="openai_invalid_response",
                retryable=True,
            ) from exc

        if result.get("success") is not True:
            reason = str(result.get("failure_reason") or "未取得完整新闻正文")
            raise ContentFetchError(
                f"CCTQ/OpenAI 网页读取失败: {reason[:1000]}",
                failure_kind="openai_web_unavailable",
                retryable=True,
            )
        full_text = self._normalize_text(str(result.get("full_text") or ""))
        if not full_text:
            raise ContentFetchError(
                "CCTQ/OpenAI 未返回新闻正文",
                failure_kind="openai_incomplete_content",
                retryable=True,
            )
        final_url_value = str(result.get("final_url") or requested_url)
        final_url = validate_public_http_url(final_url_value)
        return ContentDocument(
            requested_url=requested_url,
            final_url=final_url,
            full_text=full_text,
            content_hash=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
            content_chars=len(full_text),
            http_status=200,
            content_type="text/plain",
            extractor="openai-web-search",
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        lines = [" ".join(line.split()) for line in value.splitlines()]
        return "\n".join(line for line in lines if line).strip()
