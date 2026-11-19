"""Tool calling over the OpenAI-compatible protocol, with a prompt-based fallback.

Modes (per endpoint, ``tool_calling``):

* ``native`` - send ``tools`` and read ``tool_calls`` (vLLM with
  ``--enable-auto-tool-choice``, Ollama, llama.cpp ``--jinja``, most OpenAI-compatible
  servers).
* ``json`` - describe the tools in the prompt; the model replies with one JSON action
  object. Works with any model that can produce JSON.
* ``auto`` - native first; if the server rejects ``tools`` (HTTP 400) the endpoint is
  switched to ``json`` for the rest of the process. Tool calls a model writes as text
  (``<tool_call>{...}</tool_call>`` or a bare JSON action) are recognised in every mode.

Everything returned here is *untrusted*: the caller validates tool names and
arguments before anything runs.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.llm.gateway import (
    THINKING_HEADROOM,
    ChatMessage,
    ModelClient,
    ModelError,
    Usage,
    _with_no_think,
    describe_schema,
    extract_json,
    normalise_messages,
)

# Endpoints (api_base, model) that rejected native tools in ``auto`` mode.
_NATIVE_UNSUPPORTED: set[tuple[str, str]] = set()

_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    error: str | None = None  # arguments that could not be parsed


@dataclass(slots=True)
class ToolExchange:
    """One completed step: the calls the model made and what each returned."""

    calls: list[ToolCall]
    results: list[str]
    thought: str = ""


@dataclass(slots=True)
class ToolTurn:
    calls: list[ToolCall]
    content: str
    usage: Usage
    mode: str
    finish_reason: str | None = None
    done: bool = False

    @property
    def final(self) -> bool:
        return self.done or not self.calls


@dataclass(slots=True)
class _Parsed:
    calls: list[ToolCall] = field(default_factory=list)
    final: bool = False


def _args(raw: Any) -> tuple[dict[str, Any], str | None]:
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}, "arguments are not valid JSON"
        if isinstance(value, dict):
            return value, None
    return {}, "arguments must be a JSON object"


def _call_from(obj: Any) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # OpenAI-shaped
        obj = {**obj["function"], "id": obj.get("id")}
    if isinstance(obj.get("tool_call"), dict):
        obj = obj["tool_call"]
    name = obj.get("tool") or obj.get("name") or obj.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    args, error = _args(obj.get("arguments", obj.get("args", obj.get("parameters"))))
    return ToolCall(str(obj.get("id") or f"call_{uuid.uuid4().hex[:8]}"), name.strip(), args, error)


def parse_text_action(content: str) -> _Parsed:
    """Recognise tool calls or a final answer written as text."""
    tagged = _TOOL_CALL_TAG.findall(content or "")
    if tagged:
        calls = []
        for chunk in tagged:
            try:
                call = _call_from(json.loads(chunk))
            except ValueError:
                call = None
            if call:
                calls.append(call)
        return _Parsed(calls=calls)
    try:
        data = extract_json(content)
    except ValueError:
        return _Parsed(final=True)
    items = data if isinstance(data, list) else [data]
    calls = [c for c in (_call_from(i) for i in items) if c]
    if calls:
        return _Parsed(calls=calls)
    if isinstance(data, dict) and any(k in data for k in ("final", "done", "answer", "summary")):
        return _Parsed(final=True)
    return _Parsed(final=True)


def _json_protocol(tools: list[dict[str, Any]]) -> str:
    lines = [
        "You can call read-only diagnostic tools. Reply with exactly ONE JSON object and "
        "nothing else (no prose, no markdown):",
        '- to call a tool: {"tool": "<tool name>", "arguments": {<arguments>}}',
        '- when you have enough evidence, or nothing more would help: {"final": "<one '
        'sentence on what you found>"}',
        "Available tools:",
    ]
    for t in tools:
        fn = t["function"]
        params = describe_schema(fn["parameters"]) or "- (no arguments)"
        lines.append(
            f"* {fn['name']}: {fn['description']}\n  arguments:\n  " + params.replace("\n", "\n  ")
        )
    return "\n".join(lines)


def _render_native(
    messages: list[ChatMessage], history: list[ToolExchange]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = list(normalise_messages(messages))
    for ex in history:
        out.append(
            {
                "role": "assistant",
                "content": ex.thought or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in ex.calls
                ],
            }
        )
        for c, result in zip(ex.calls, ex.results, strict=True):
            out.append({"role": "tool", "tool_call_id": c.id, "content": result})
    return out


def _render_json(
    messages: list[ChatMessage], history: list[ToolExchange], tools: list[dict[str, Any]]
) -> list[ChatMessage]:
    convo = [*messages, ChatMessage("system", _json_protocol(tools))]
    for ex in history:
        for c, result in zip(ex.calls, ex.results, strict=True):
            convo.append(
                ChatMessage("assistant", json.dumps({"tool": c.name, "arguments": c.arguments}))
            )
            convo.append(ChatMessage("user", f"Result of {c.name}:\n{result}"))
    if history:
        convo.append(
            ChatMessage("user", "Call the next tool, or reply with the final object if done.")
        )
    return convo


def effective_mode(client: ModelClient) -> str:
    ep = client.endpoint
    if ep.tool_calling == "auto" and (ep.api_base, ep.model) in _NATIVE_UNSUPPORTED:
        return "json"
    return "native" if ep.tool_calling in ("auto", "native") else "json"


async def chat_tools(
    client: ModelClient,
    messages: list[ChatMessage],
    tools: list[dict[str, Any]],
    history: list[ToolExchange],
    *,
    temperature: float = 0.1,
    max_tokens: int = 1024,
    request_timeout: float | None = None,
) -> ToolTurn:
    ep = client.endpoint
    budget = max_tokens
    if ep.thinking != "disabled":
        budget = max(budget, THINKING_HEADROOM)
    base = _with_no_think(messages) if ep.thinking == "disabled" else messages
    mode = effective_mode(client)

    if mode == "native":
        try:
            result = await client.chat(
                base,
                temperature=temperature,
                max_tokens=budget,
                request_timeout=request_timeout,
                raw_messages=_render_native(base, history),
                extra={"tools": tools, "tool_choice": "auto"},
            )
        except ModelError as exc:
            if ep.tool_calling == "auto" and exc.status == 400:
                _NATIVE_UNSUPPORTED.add((ep.api_base, ep.model))
                mode = "json"
            else:
                raise
        else:
            calls = []
            for raw in result.tool_calls:
                args, error = _args(raw.get("arguments"))
                calls.append(
                    ToolCall(
                        raw.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        raw.get("name", ""),
                        args,
                        error,
                    )
                )
            if not calls and result.content:
                calls = parse_text_action(result.content).calls
            return ToolTurn(calls, result.content, result.usage, "native", result.finish_reason)

    response_format = {"type": "json_object"} if ep.structured_output != "none" else None
    result = await client.chat(
        _render_json(base, history, tools),
        temperature=temperature,
        max_tokens=budget,
        response_format=response_format,
        request_timeout=request_timeout,
    )
    parsed = parse_text_action(result.content)
    return ToolTurn(
        parsed.calls,
        result.content,
        result.usage,
        "json",
        result.finish_reason,
        done=parsed.final and not parsed.calls,
    )
