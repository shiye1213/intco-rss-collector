from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
