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
ThinkingMode = Literal["auto", "enabled", "disabled"]

# Extra completion tokens reserved for the reasoning of thinking models.
THINKING_HEADROOM = 4096
MAX_STRUCTURED_TOKENS = 8192

# Allows tests (and future in-process runtimes) to inject an httpx transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class ModelError(Exception):
    """A model call failed (unreachable, HTTP error, unusable output)."""

    def __init__(self, message: str, *, retryable: bool = False, detail: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        # Excerpt of model output shown to operators (stored with the investigation,
        # never logged).
        self.detail = detail


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
    thinking: ThinkingMode = "auto"

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
            thinking=cfg.get("thinking", "auto"),
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
    lowered = cleaned.lower()
    if "</think>" in lowered and "<think>" not in lowered:
        # Templates that open the reasoning block in the prompt (Qwen3, DeepSeek-R1)
        # return only its closing tag.
        cleaned = cleaned[lowered.rfind("</think>") + len("</think>") :].strip()
        lowered = cleaned.lower()
    if "<think>" in lowered and "</think>" not in lowered:
        head, tail = (
            cleaned[: lowered.rfind("<think>")].strip(),
            cleaned[lowered.rfind("<think>") :],
        )
        if not head and "{" not in tail:
            raise ValueError("reply ended inside a <think> reasoning block before any JSON")
        cleaned = head or tail
    if not cleaned:
        raise ValueError("reply was empty (the model may have used its whole budget thinking)")
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


def _thinking_extras(ep: EndpointConfig) -> dict[str, Any]:
    """Request-level switches that turn reasoning off on common servers."""
    if ep.thinking != "disabled":
        return {}
    if ep.provider == "ollama":
        return {"think": False}
    # vLLM / SGLang / llama.cpp server pass these to the chat template (Qwen3 etc.).
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _with_no_think(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Append Qwen3's ``/no_think`` soft switch to the last user message."""
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].role == "user":
            out[i] = ChatMessage("user", out[i].content.rstrip() + "\n/no_think")
            break
    return out


def describe_schema(schema: dict[str, Any]) -> str:
    """Human-readable field list for a Pydantic JSON Schema (top level)."""
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    lines = []
    for name, prop in schema.get("properties", {}).items():
        suffix = "" if name in required else " (optional)"
        lines.append(f"- {name}: {_describe_type(prop, defs)}{suffix}")
    return "\n".join(lines)


def _describe_type(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop:
        target = defs.get(prop["$ref"].split("/")[-1], {})
        if target.get("properties"):
            inner = ", ".join(
                f"{k} ({_describe_type(v, defs)})" for k, v in target["properties"].items()
            )
            return f"object with {inner}"
        return _describe_type(target, defs)
    if "enum" in prop:
        return "one of " + ", ".join(json.dumps(v) for v in prop["enum"])
    if "anyOf" in prop:
        kinds = [_describe_type(p, defs) for p in prop["anyOf"]]
        return " or ".join(dict.fromkeys(kinds))
    kind = prop.get("type", "any")
    if kind == "array":
        return f"array of {_describe_type(prop.get('items', {}), defs)}"
    names = {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "true or false",
        "null": "null",
        "object": "object",
    }
    base = names.get(kind, str(kind))
    desc = prop.get("description")
    return f"{base} ({desc})" if desc else base


def unwrap_object(data: Any, expected: set[str]) -> Any:
    """Undo common wrappers such as ``[{...}]`` or ``{"Classification": {...}}``."""
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    for _ in range(2):
        if (
            isinstance(data, dict)
            and expected
            and not (expected & set(data))
            and len(data) == 1
            and isinstance(next(iter(data.values())), dict)
        ):
            data = next(iter(data.values()))
    return data


def _output_excerpt(content: str, limit: int = 300) -> str:
    text = " ".join(content.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        """One chat completion.

        Responses are streamed, so the endpoint timeout bounds the *silence* between
        tokens rather than the whole generation: slow or thinking models keep going as
        long as they produce output. (The investigation has its own overall limit.)
        Servers that answer with a plain JSON body are handled too.
        """
        ep = self.endpoint
        payload: dict[str, Any] = {
            "model": ep.model,
            "messages": normalise_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if response_format:
            payload["response_format"] = response_format
        payload.update(_thinking_extras(ep))
        # Fields some strict servers reject; dropped (once) on HTTP 400.
        optional = [k for k in ("stream_options", *_thinking_extras(ep)) if k in payload]
        idle = request_timeout or ep.timeout_seconds

        attempts = ep.max_retries + 1
        last_error: ModelError | None = None
        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            try:
                status, error_text, parsed = await self._send(payload, request_timeout)
                if status == 400 and optional:
                    log.info("model_optional_fields_rejected", model=ep.model, fields=optional)
                    for key in optional:
                        payload.pop(key, None)
                    optional = []
                    status, error_text, parsed = await self._send(payload, request_timeout)
                elapsed = time.perf_counter() - start
                if status == 429 or status >= 500:
                    raise ModelError(f"Model endpoint returned HTTP {status}", retryable=True)
                if status >= 400 or parsed is None:
                    raise ModelError(
                        f"Model endpoint rejected the request (HTTP {status}): {error_text}"
                    )
                raw_usage = parsed["usage"] or {}
                usage = Usage(
                    prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(raw_usage.get("completion_tokens") or 0),
                    calls=1,
                )
                self._record("success", elapsed, usage)
                return ChatResult(
                    content=parsed["content"],
                    usage=usage,
                    latency_ms=round(elapsed * 1000, 1),
                    finish_reason=parsed["finish_reason"],
                )
            except httpx.ReadTimeout:
                # Re-sending would only wait the same way again.
                last_error = ModelError(
                    f"Model produced no output for {idle:.0f} s (read timeout). Increase "
                    "the endpoint timeout in Settings > AI Models, or check the model server."
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

    async def _send(
        self, payload: dict[str, Any], request_timeout: float | None
    ) -> tuple[int, str, dict[str, Any] | None]:
        """POST once; returns (status, error text, normalised completion or None)."""
        url = f"{self.endpoint.api_base}/chat/completions"
        async with (
            self._client(request_timeout) as client,
            client.stream("POST", url, json=payload) as resp,
        ):
            if resp.status_code >= 400:
                await resp.aread()
                return resp.status_code, _error_snippet(resp), None
            if "text/event-stream" in resp.headers.get("content-type", ""):
                return resp.status_code, "", await _read_event_stream(resp)
            await resp.aread()
            return resp.status_code, "", _parse_completion(resp.json())

    async def structured[T: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        repair_attempts: int = 2,
        request_timeout: float | None = None,
        example: dict[str, Any] | None = None,
    ) -> StructuredResult[T]:
        """Request a JSON object matching ``schema`` and validate it.

        Local models follow a field list and a concrete example far better than a raw
        JSON Schema. Invalid output is sent back with the problems, up to
        ``repair_attempts`` times; output cut off by the token limit (typical for
        thinking models) is retried with a larger budget. Nothing bypasses validation.
        """
        ep = self.endpoint
        json_schema = schema.model_json_schema()
        response_format: dict[str, Any] | None = None
        if ep.structured_output == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": False},
            }
        elif ep.structured_output == "json_object":
            response_format = {"type": "json_object"}

        instruction = (
            "Output format: after any reasoning, reply with ONE JSON object and nothing "
            "else - no prose, no markdown, no code fences, and do not repeat this "
            f"description.\nFields:\n{describe_schema(json_schema)}"
        )
        if example is not None:
            instruction += f"\nExample of the expected shape:\n{json.dumps(example)}"
        convo = [*messages, ChatMessage("system", instruction)]
        if ep.thinking == "disabled":
            convo = _with_no_think(convo)
        budget = max_tokens
        if ep.thinking != "disabled":
            # A completion limit is a ceiling, not a target: generous headroom costs
            # nothing for plain models and stops reasoning models being cut off.
            budget = max(budget, THINKING_HEADROOM)
        if ep.thinking == "enabled":
            budget += THINKING_HEADROOM
        total = Usage()
        problem = "no output"
        content = ""
        for attempt in range(1, repair_attempts + 2):
            result = await self.chat(
                convo,
                temperature=temperature,
                max_tokens=budget,
                response_format=response_format,
                request_timeout=request_timeout,
            )
            total.add(result.usage)
            content = result.content
            try:
                data = unwrap_object(extract_json(content), set(json_schema.get("properties", {})))
                value = schema.model_validate(data)
                return StructuredResult(value=value, usage=total, attempts=attempt)
            except (ValueError, ValidationError) as exc:
                problem = _compact_errors(exc) if isinstance(exc, ValidationError) else str(exc)
                if result.finish_reason == "length":
                    problem = f"reply was cut off at the {budget}-token limit; {problem}"
                    budget = min(budget * 2, MAX_STRUCTURED_TOKENS)
                log.info(
                    "model_output_invalid", model=ep.model, attempt=attempt, problem=problem[:300]
                )
                convo = [
                    *convo,
                    ChatMessage("assistant", _THINK.sub("", content)[:4000] or "(empty)"),
                    ChatMessage(
                        "user",
                        f"That reply was not valid ({problem}). Reply again with only the "
                        "corrected JSON object containing the listed fields.",
                    ),
                ]
        raise ModelError(
            f"Model '{ep.model}' did not return valid structured output after "
            f"{repair_attempts + 1} attempts: {problem[:300]}",
            detail=_output_excerpt(content),
        )

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


def _parse_completion(data: dict[str, Any]) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    # Reasoning servers (vLLM, SGLang, LM Studio, Ollama) may return the chain of
    # thought separately (reasoning_content); only ``content`` is the answer.
    return {
        "content": message.get("content") or "",
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage"),
    }


async def _read_event_stream(resp: httpx.Response) -> dict[str, Any]:
    """Accumulate an OpenAI-style SSE completion stream."""
    parts: list[str] = []
    finish: str | None = None
    usage: dict[str, Any] | None = None
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("error"):
            err = chunk["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise ModelError(f"Model endpoint error: {str(msg)[:200]}")
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return {"content": "".join(parts), "finish_reason": finish, "usage": usage}


def _error_snippet(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error", body)
        if isinstance(msg, dict):
            msg = msg.get("message", msg)
        return str(msg)[:200]
    except ValueError:
        return resp.text[:200]
