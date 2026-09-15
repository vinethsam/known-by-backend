"""OpenRouter structured completion client."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Self, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas import UsageRecord

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
T = TypeVar("T", bound=BaseModel)
UsageCallback = Callable[[UsageRecord], None | Awaitable[None]]
AttemptCallback = Callable[[dict[str, Any]], None | Awaitable[None]]


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter cannot return a valid structured response."""

    def __init__(self, message: str, *, usage: list[UsageRecord] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or []


class OpenRouterResponseTooLarge(OpenRouterError):
    """Raised when a provider response exceeds the configured byte cap."""


class OpenRouterClient:
    """Small async client for role-scoped structured completions."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        usage_callback: UsageCallback | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._usage_callback = usage_callback
        self._sleep = sleep or asyncio.sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.OPENROUTER_TIMEOUT_SECONDS)
        return self._client

    async def complete(
        self,
        schema: type[T],
        role: str,
        prompt: str,
        payload: dict,
        context: dict,
        prompt_version: str,
        *,
        before_attempt: AttemptCallback | None = None,
        on_usage: UsageCallback | None = None,
    ) -> tuple[T, list[UsageRecord]]:
        """Return a parsed model and all usage attempts made for the call."""

        model = self._model_for_role(role)
        if not self.settings.OPENROUTER_API_KEY:
            raise OpenRouterError("OPENROUTER_API_KEY is required")

        attempt_usage: list[UsageRecord] = []
        last_error: Exception | None = None
        for attempt in range(self.settings.OPENROUTER_MAX_RETRIES + 1):
            await self._emit_attempt(
                before_attempt,
                {
                    "attempt": attempt,
                    "max_retries": self.settings.OPENROUTER_MAX_RETRIES,
                    "job_id": context.get("job_id"),
                    "person_id": context.get("person_id"),
                    "source_id": context.get("source_id"),
                    "role": role,
                    "model": model,
                    "prompt_version": prompt_version,
                },
            )
            started = time.monotonic()
            try:
                request = self.client.build_request(
                    "POST",
                    OPENROUTER_CHAT_COMPLETIONS_URL,
                    headers=self._headers(),
                    json=self._request_body(schema, model, prompt, payload, context),
                )
                response = await self.client.send(request, stream=True)
                try:
                    data = await self._read_json_response(response)
                except OpenRouterError:
                    usage = self._usage_from_failure(
                        role=role,
                        model=model,
                        prompt_version=prompt_version,
                        context=context,
                        latency_ms=(time.monotonic() - started) * 1000,
                    )
                    attempt_usage.append(usage)
                    await self._emit_usage(usage, on_usage)
                    raise
                usage = self._usage_from_response(
                    data=data,
                    role=role,
                    model=model,
                    prompt_version=prompt_version,
                    context=context,
                    latency_ms=(time.monotonic() - started) * 1000,
                    success=False,
                )
                attempt_usage.append(usage)

                if self._retryable_status(response.status_code):
                    await self._emit_usage(usage, on_usage)
                    raise OpenRouterError(
                        f"OpenRouter retryable HTTP {response.status_code}", usage=attempt_usage
                    )
                if response.status_code >= 400:
                    await self._emit_usage(usage, on_usage)
                response.raise_for_status()
                try:
                    parsed = self._parse_message(schema, data)
                except (json.JSONDecodeError, KeyError, TypeError, ValidationError, OpenRouterError):
                    await self._emit_usage(usage, on_usage)
                    raise
                usage.success = True
                await self._emit_usage(usage, on_usage)
                return parsed, attempt_usage
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                usage = self._usage_from_failure(
                    role=role,
                    model=model,
                    prompt_version=prompt_version,
                    context=context,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                attempt_usage.append(usage)
                await self._emit_usage(usage, on_usage)
            except (json.JSONDecodeError, KeyError, TypeError, ValidationError, OpenRouterError) as exc:
                last_error = exc
                if isinstance(exc, OpenRouterError) and not self._should_retry_openrouter_error(exc):
                    raise OpenRouterError(str(exc), usage=attempt_usage) from exc
            except httpx.HTTPStatusError as exc:
                raise OpenRouterError(
                    f"OpenRouter HTTP {exc.response.status_code}", usage=attempt_usage
                ) from exc

            if attempt >= self.settings.OPENROUTER_MAX_RETRIES:
                break
            await self._sleep(self._retry_delay(attempt))

        raise OpenRouterError("OpenRouter failed after bounded retries", usage=attempt_usage) from last_error

    def _model_for_role(self, role: str) -> str:
        if role == "source":
            model = self.settings.OPENROUTER_SOURCE_MODEL
        elif role == "extraction":
            model = self.settings.OPENROUTER_EXTRACTION_MODEL
        else:
            raise OpenRouterError(f"Unsupported OpenRouter role: {role}")
        if not model:
            raise OpenRouterError(f"OpenRouter model is required for role: {role}")
        return model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY.get_secret_value()}",
            "Content-Type": "application/json",
        }

    def _request_body(
        self,
        schema: type[BaseModel],
        model: str,
        prompt: str,
        payload: dict,
        context: dict,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"payload": payload, "context": context},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": self.settings.OPENROUTER_TEMPERATURE,
            "max_tokens": self.settings.OPENROUTER_MAX_TOKENS,
            "stream": False,
        }
        if self.settings.OPENROUTER_RESPONSE_FORMAT == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(schema),
                },
            }
            body["provider"] = {"require_parameters": True}
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    @staticmethod
    def _should_retry_openrouter_error(exc: OpenRouterError) -> bool:
        message = str(exc)
        return (
            "retryable HTTP" in message
            or "empty choices" in message
            or "empty content" in message
            or "malformed" in message
            or "too large" in message
            or "schema mismatch" in message
        )

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(12.0, 0.75 * (2**attempt)) + random.uniform(0, 0.2)

    @staticmethod
    def _parse_message(schema: type[T], data: dict) -> T:
        if not isinstance(data, dict):
            raise OpenRouterError("OpenRouter returned malformed JSON envelope")
        choices = data.get("choices") or []
        if not isinstance(choices, list):
            raise OpenRouterError("OpenRouter returned malformed choices")
        if not choices:
            raise OpenRouterError("OpenRouter returned empty choices")
        message = (choices[0].get("message") or {}) if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            raise OpenRouterError("OpenRouter returned malformed message")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterError("OpenRouter returned empty content")
        try:
            parsed_json = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter returned malformed JSON content") from exc
        try:
            return schema.model_validate(parsed_json)
        except ValidationError as exc:
            raise OpenRouterError("OpenRouter returned schema mismatch") from exc

    def _usage_from_response(
        self,
        *,
        data: Any,
        role: str,
        model: str,
        prompt_version: str,
        context: dict,
        latency_ms: float,
        success: bool,
    ) -> UsageRecord:
        usage_data: dict[str, Any] = {}
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage_data = data["usage"]
        return UsageRecord(
            job_id=str(context.get("job_id") or "unknown"),
            person_id=str(context.get("person_id") or "unknown"),
            source_id=context.get("source_id"),
            role=role,
            model=model,
            prompt_version=prompt_version,
            prompt_tokens=_token_count(usage_data.get("prompt_tokens")),
            completion_tokens=_token_count(usage_data.get("completion_tokens")),
            cost=_reported_cost(usage_data.get("cost")),
            latency_ms=latency_ms,
            success=success,
        )

    @staticmethod
    def _usage_from_failure(
        *,
        role: str,
        model: str,
        prompt_version: str,
        context: dict,
        latency_ms: float,
    ) -> UsageRecord:
        return UsageRecord(
            job_id=str(context.get("job_id") or "unknown"),
            person_id=str(context.get("person_id") or "unknown"),
            source_id=context.get("source_id"),
            role=role,
            model=model,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            success=False,
        )

    async def _read_json_response(self, response: httpx.Response) -> Any:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.settings.MAX_RESPONSE_BYTES:
                    raise OpenRouterResponseTooLarge("OpenRouter response too large")
                chunks.append(chunk)
        finally:
            await response.aclose()
        raw = b"".join(chunks)
        if not raw.strip():
            raise OpenRouterError("OpenRouter returned empty response")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OpenRouterError("OpenRouter returned malformed JSON envelope") from exc

    @staticmethod
    async def _emit_attempt(callback: AttemptCallback | None, attempt: dict[str, Any]) -> None:
        if not callback:
            return
        result = callback(attempt)
        if inspect.isawaitable(result):
            await result

    async def _emit_usage(self, usage: UsageRecord, on_usage: UsageCallback | None) -> None:
        for callback in (self._usage_callback, on_usage):
            if not callback:
                continue
            result = callback(usage)
            if inspect.isawaitable(result):
                await result


def _token_count(value: Any) -> int:
    # Unknown usage must not discard a paid attempt or release its reservation.
    if isinstance(value, bool):
        return 0
    try:
        numeric = float(value)
        return int(numeric) if math.isfinite(numeric) and numeric >= 0 and numeric.is_integer() else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _reported_cost(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    _stricten_schema_node(schema)
    return schema


def _stricten_schema_node(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties") or {}
            node["additionalProperties"] = False
            node["required"] = list(properties.keys())
        for value in node.values():
            _stricten_schema_node(value)
    elif isinstance(node, list):
        for item in node:
            _stricten_schema_node(item)
