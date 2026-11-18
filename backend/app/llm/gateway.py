"""Model gateway: the only path from LocalOps to a language model (spec §14).

Speaks the OpenAI-compatible ``/v1/chat/completions`` protocol, which vLLM, Ollama,
llama.cpp's server and most other self-hosted runtimes implement. Deliberately has no
LangChain/OpenAI SDK dependency so nothing downloads tokenizers or phones home.

Responsibilities: endpoint normalisation, timeouts, bounded retries, structured (JSON)
output with validation and repair, token accounting, metrics and redacted logging.
Prompt and completion *contents* are never logged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger

log = get_logger("localops.llm")

MODEL_REQUESTS = Counter(
    "localops_model_requests_total", "Model requests", ["provider", "model", "outcome"]
)
MODEL_LATENCY = Histogram(
    "localops_model_request_duration_seconds",
    "Model request latency",
    ["provider", "model"],
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)
MODEL_TOKENS = Counter("localops_model_tokens_total", "Model tokens", ["provider", "model", "kind"])

Provider = Literal["vllm", "ollama", "llamacpp", "openai_compatible"]
StructuredMode = Literal["json_schema", "json_object", "none"]

# Allows tests (and future in-process runtimes) to inject an httpx transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class ModelError(Exception):
    """A model call failed (unreachable, HTTP error, unusable output)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    provider: Provider
    base_url: str
    model: str
    name: str = "model"
    api_key: str | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 2
    structured_output: StructuredMode = "json_object"
    context_window: int | None = None

    @property
    def api_base(self) -> str:
        """Normalise to the OpenAI-compatible root, e.g. ``http://host:8000/v1``."""
        base = self.base_url.rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    @property
    def native_root(self) -> str:
        base = self.base_url.rstrip("/")
        return base[: -len("/v1")] if base.endswith("/v1") else base

    @classmethod
    def from_record(
        cls,
        *,
        provider: str,
        base_url: str,
        model_name: str,
        name: str,
        configuration: dict[str, Any],
        api_key: str | None,
    ) -> EndpointConfig:
        cfg = configuration or {}
        return cls(
            provider=provider,  # type: ignore[arg-type]
            base_url=base_url,
            model=model_name,
            name=name,
            api_key=api_key,
            timeout_seconds=float(cfg.get("timeout_seconds", 120)),
            max_retries=int(cfg.get("max_retries", 2)),
            structured_output=cfg.get("structured_output", "json_object"),
            context_window=cfg.get("context_window"),
        )


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls


@dataclass(slots=True)
class ChatResult:
    content: str
    usage: Usage
    latency_ms: float
    finish_reason: str | None = None


@dataclass(slots=True)
class StructuredResult[T: BaseModel]:
    value: T
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class HealthResult:
    ok: bool
    latency_ms: float | None
    models: list[str]
    model_available: bool | None
    reply: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- JSON helpers

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Best-effort extraction of one JSON object from model output.

    Handles reasoning blocks (``<think>``), markdown fences and leading/trailing prose.
    """
    cleaned = _THINK.sub("", text).strip()
    fenced = _FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : i + 1])
    raise ValueError("Unterminated JSON object in model output")


def _compact_errors(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in err.errors()[:8]
    )


# --------------------------------------------------------------------------- gateway


class ModelClient:
    """Client for one configured endpoint."""

    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.endpoint = endpoint
        self._transport = transport or _transport_override
        self._sleep = sleep

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(timeout or self.endpoint.timeout_seconds, connect=10.0),
            headers=headers,
            trust_env=False,  # never route model traffic through ambient proxies
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> ChatResult:
        ep = self.endpoint
        payload: dict[str, Any] = {
            "model": ep.model,
            "messages": normalise_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        attempts = ep.max_retries + 1
        last_error: ModelError | None = None
        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            try:
                async with self._client(request_timeout) as client:
                    resp = await client.post(f"{ep.api_base}/chat/completions", json=payload)
                elapsed = time.perf_counter() - start
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ModelError(
                        f"Model endpoint returned HTTP {resp.status_code}", retryable=True
                    )
                if resp.status_code >= 400:
                    raise ModelError(
                        f"Model endpoint rejected the request (HTTP {resp.status_code}): "
                        f"{_error_snippet(resp)}"
                    )
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                usage_raw = data.get("usage") or {}
                usage = Usage(
                    prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                    calls=1,
                )
                self._record("success", elapsed, usage)
                return ChatResult(
                    content=content,
                    usage=usage,
                    latency_ms=round(elapsed * 1000, 1),
                    finish_reason=choice.get("finish_reason"),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ModelError(
                    f"Model endpoint unreachable: {type(exc).__name__}", retryable=True
                )
            except ModelError as exc:
                last_error = exc
            except (ValueError, KeyError, TypeError) as exc:
                last_error = ModelError(f"Malformed model response: {type(exc).__name__}")
            self._record("error", time.perf_counter() - start, None)
            log.warning(
                "model_request_failed",
                endpoint=ep.name,
                model=ep.model,
                attempt=attempt,
                error=str(last_error),
            )
            if not last_error.retryable or attempt == attempts:
                break
            await self._sleep(min(0.5 * 2 ** (attempt - 1), 8.0))
        assert last_error is not None  # noqa: S101
        raise last_error

    async def structured[T: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        repair_attempts: int = 1,
        request_timeout: float | None = None,
    ) -> StructuredResult[T]:
        """Request a JSON object matching ``schema`` and validate it.

        Invalid output is sent back to the model with the validation errors, up to
        ``repair_attempts`` times. The model never gets to decide anything that
        bypasses this validation.
        """
        json_schema = schema.model_json_schema()
        mode = self.endpoint.structured_output
        response_format: dict[str, Any] | None = None
        if mode == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": False},
            }
        elif mode == "json_object":
            response_format = {"type": "json_object"}

        instruction = (
            "Respond with a single JSON object only, no prose and no markdown, that validates "
            f"against this JSON Schema:\n{json.dumps(json_schema, separators=(',', ':'))}"
        )
        convo = [*messages, ChatMessage("system", instruction)]
        total = Usage()
        for attempt in range(1, repair_attempts + 2):
            result = await self.chat(
                convo,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                request_timeout=request_timeout,
            )
            total.add(result.usage)
            try:
                value = schema.model_validate(extract_json(result.content))
                return StructuredResult(value=value, usage=total, attempts=attempt)
            except (ValueError, ValidationError) as exc:
                problem = _compact_errors(exc) if isinstance(exc, ValidationError) else str(exc)
                log.info(
                    "model_output_invalid",
                    model=self.endpoint.model,
                    attempt=attempt,
                    problem=problem[:300],
                )
                convo = [
                    *convo,
                    ChatMessage("assistant", result.content[:4000]),
                    ChatMessage(
                        "user",
                        f"That output was invalid ({problem}). Reply again with only the "
                        "corrected JSON object.",
                    ),
                ]
        raise ModelError("Model did not return valid structured output")

    async def health(self, *, probe: str | None = "Reply with the single word OK.") -> HealthResult:
        """List models and optionally run a tiny completion."""
        ep = self.endpoint
        start = time.perf_counter()
        models: list[str] = []
        try:
            async with self._client(timeout=min(ep.timeout_seconds, 15.0)) as client:
                if ep.provider == "ollama":
                    resp = await client.get(f"{ep.native_root}/api/tags")
                    resp.raise_for_status()
                    models = [m.get("name", "") for m in resp.json().get("models", [])]
                else:
                    resp = await client.get(f"{ep.api_base}/models")
                    resp.raise_for_status()
                    models = [m.get("id", "") for m in resp.json().get("data", [])]
        except (httpx.HTTPError, ValueError) as exc:
            return HealthResult(
                False, None, [], None, error=f"Model list failed: {type(exc).__name__}"
            )
        available = ep.model in models if models else None
        reply = None
        if probe:
            try:
                result = await ModelClient(
                    dataclasses.replace(ep, max_retries=0), transport=self._transport
                ).chat([ChatMessage("user", probe)], max_tokens=16, temperature=0)
                reply = _THINK.sub("", result.content).strip()[:200]
            except ModelError as exc:
                return HealthResult(False, None, models, available, error=str(exc))
        return HealthResult(
            True, round((time.perf_counter() - start) * 1000, 1), models, available, reply=reply
        )

    def _record(self, outcome: str, elapsed: float, usage: Usage | None) -> None:
        ep = self.endpoint
        MODEL_REQUESTS.labels(ep.provider, ep.model, outcome).inc()
        MODEL_LATENCY.labels(ep.provider, ep.model).observe(elapsed)
        if usage:
            MODEL_TOKENS.labels(ep.provider, ep.model, "prompt").inc(usage.prompt_tokens)
            MODEL_TOKENS.labels(ep.provider, ep.model, "completion").inc(usage.completion_tokens)
            log.info(
                "model_request",
                endpoint=ep.name,
                model=ep.model,
                latency_ms=round(elapsed * 1000, 1),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )


def normalise_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Merge every system message into one leading system message.

    Many chat templates (Mistral, Gemma, some Qwen/vLLM setups) reject a system
    message anywhere but first, or more than one of them. Other messages keep their
    order, so user/assistant alternation is preserved.
    """
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    rest = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return ([{"role": "system", "content": system}] if system else []) + rest


def _error_snippet(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error", body)
        if isinstance(msg, dict):
            msg = msg.get("message", msg)
        return str(msg)[:200]
    except ValueError:
        return resp.text[:200]
