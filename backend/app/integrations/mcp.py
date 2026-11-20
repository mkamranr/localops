"""Minimal Model Context Protocol client for remote (network) MCP servers.

Supports the Streamable HTTP transport (JSON or SSE responses, ``Mcp-Session-Id``) and
falls back to the legacy HTTP+SSE transport. Only what LocalOps needs: ``initialize``,
``tools/list`` and ``tools/call``. LocalOps never launches MCP servers as processes.

Security notes:
- the server URL and auth headers come from an admin-configured integration;
- redirects are not followed and ambient proxy settings are ignored;
- tool descriptions/schemas from a server are untrusted: the tool sync pins them and
  an admin reviews every tool before it can be used (``app.services.tool_service``);
- responses are size-capped before anything parses them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx

from app.core.logging import get_logger

log = get_logger("localops.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "localops", "version": "0.6"}
MAX_RESPONSE_BYTES = 2_000_000
MAX_TOOLS = 500

# Tests inject an httpx transport (e.g. ``httpx.ASGITransport`` over a fake server).
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class McpError(Exception):
    def __init__(self, message: str, *, code: str = "mcp_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class McpConfig:
    url: str
    transport: Literal["auto", "streamable_http", "sse"] = "auto"
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    ca_pem: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallResult:
    text: str
    is_error: bool
    structured: Any = None


def _verify(config: McpConfig) -> ssl.SSLContext | bool:
    if not config.verify_tls:
        return False
    if config.ca_pem:
        return ssl.create_default_context(cadata=config.ca_pem)
    return True


def _sse_events(text: str) -> list[tuple[str, str]]:
    """Parse a complete SSE body into (event, data) pairs."""
    events: list[tuple[str, str]] = []
    data: list[str] = []
    event = "message"
    for line in text.splitlines():
        if not line:
            if data:
                events.append((event, "\n".join(data)))
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        events.append((event, "\n".join(data)))
    return events


def _pick_response(messages: list[Any], request_id: int) -> dict[str, Any] | None:
    for msg in messages:
        batch = msg if isinstance(msg, list) else [msg]
        for item in batch:
            if (
                isinstance(item, dict)
                and item.get("id") == request_id
                and ("result" in item or "error" in item)
            ):
                return item
    return None


class McpClient:
    """One session with one MCP server. Use as ``async with McpClient(cfg) as c``."""

    def __init__(self, config: McpConfig) -> None:
        parsed = urlparse(config.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise McpError("The MCP server URL must be an http(s) URL", code="invalid_url")
        self.config = config
        self._http: httpx.AsyncClient | None = None
        self._next_id = 0
        self._session_id: str | None = None
        self._protocol = PROTOCOL_VERSION
        self._mode: str | None = None  # streamable_http | sse
        self.server_info: dict[str, Any] = {}
        # legacy SSE state
        self._sse_endpoint: str | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_waiters: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sse_ready: asyncio.Event | None = None

    async def __aenter__(self) -> McpClient:
        cfg = self.config
        self._http = httpx.AsyncClient(
            transport=_transport_override,
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=min(cfg.timeout_seconds, 10.0)),
            headers={**cfg.headers, "User-Agent": "LocalOps-MCP/0.6"},
            verify=_verify(cfg),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            await self.initialize()
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sse_task
        if self._http is not None:
            if self._session_id and self._mode == "streamable_http":
                with contextlib.suppress(httpx.HTTPError):
                    await self._http.delete(self.config.url, headers=self._headers())
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ protocol

    async def initialize(self) -> dict[str, Any]:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        mode = self.config.transport
        if mode in ("auto", "streamable_http"):
            try:
                self._mode = "streamable_http"
                result = await self._request("initialize", params)
            except McpError as exc:
                if mode == "streamable_http" or exc.code not in (
                    "http_404",
                    "http_405",
                    "http_400",
                ):
                    raise
                log.info("mcp_fallback_to_sse", url=self.config.url)
                self._mode = None
            else:
                return await self._initialized(result)
        self._mode = "sse"
        await self._open_sse()
        return await self._initialized(await self._request("initialize", params))

    async def _initialized(self, result: dict[str, Any]) -> dict[str, Any]:
        self._protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = dict(result.get("serverInfo") or {})
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[McpTool]:
        tools: list[McpTool] = []
        cursor: str | None = None
        for _ in range(50):  # pagination guard
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            for raw in result.get("tools") or []:
                if not isinstance(raw, dict) or not raw.get("name"):
                    continue
                schema = raw.get("inputSchema")
                tools.append(
                    McpTool(
                        name=str(raw["name"])[:200],
                        title=(str(raw["title"])[:200] if raw.get("title") else None),
                        description=str(raw.get("description") or "")[:4000],
                        input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                        output_schema=raw.get("outputSchema")
                        if isinstance(raw.get("outputSchema"), dict)
                        else None,
                        annotations=dict(raw["annotations"])
                        if isinstance(raw.get("annotations"), dict)
                        else {},
                    )
                )
                if len(tools) >= MAX_TOOLS:
                    return tools
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallResult:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "resource":
                res = block.get("resource") or {}
                parts.append(str(res.get("text") or f"[resource {res.get('uri', '')}]"))
            elif kind == "resource_link":
                parts.append(f"[resource link {block.get('uri', '')}]")
            else:
                parts.append(f"[{kind} content omitted]")
        structured = result.get("structuredContent")
        if structured is not None and not parts:
            parts.append(json.dumps(structured, indent=1, default=str))
        return CallResult("\n".join(parts), bool(result.get("isError")), structured)

    # ------------------------------------------------------------------ transport

    def _headers(self) -> dict[str, str]:
        headers = {"MCP-Protocol-Version": self._protocol}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise McpError("The MCP client is closed", code="closed")
        return self._http

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        reply = await (self._post_sse(message) if self._mode == "sse" else self._post(message))
        if reply is None:
            raise McpError(f"The MCP server sent no response to {method}", code="no_response")
        if "error" in reply:
            err = reply["error"] or {}
            raise McpError(
                f"MCP error {err.get('code')}: {str(err.get('message') or '')[:300]}",
                code="rpc_error",
            )
        result = reply.get("result")
        if not isinstance(result, dict):
            raise McpError(f"Malformed MCP result for {method}", code="malformed")
        return result

    async def _notify(self, method: str) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if self._mode == "sse":
            await self._send_sse(message)
            return
        with contextlib.suppress(McpError):
            await self._post(message, expect_reply=False)

    async def _post(
        self, message: dict[str, Any], *, expect_reply: bool = True
    ) -> dict[str, Any] | None:
        headers = {
            **self._headers(),
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if message.get("method") == "initialize":
            headers.pop("MCP-Protocol-Version", None)
        try:
            async with self._client().stream(
                "POST", self.config.url, json=message, headers=headers
            ) as resp:
                if 300 <= resp.status_code < 400:
                    raise McpError(
                        "The MCP server redirected; redirects are not followed", code="redirect"
                    )
                if resp.status_code >= 400:
                    raise McpError(
                        f"The MCP server returned HTTP {resp.status_code}",
                        code=f"http_{resp.status_code}",
                    )
                if sid := resp.headers.get("mcp-session-id"):
                    self._session_id = sid[:200]
                if not expect_reply or resp.status_code == 202:
                    return None
                body = await self._read_capped(resp)
                ctype = resp.headers.get("content-type", "")
        except httpx.TimeoutException as exc:
            raise McpError("The MCP server did not answer in time", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise McpError(
                f"Cannot reach the MCP server: {type(exc).__name__}", code="unreachable"
            ) from exc
        try:
            if "text/event-stream" in ctype:
                messages = [json.loads(d) for e, d in _sse_events(body) if e == "message" and d]
            else:
                messages = [json.loads(body)] if body.strip() else []
        except json.JSONDecodeError as exc:
            raise McpError("The MCP server sent invalid JSON", code="malformed") from exc
        return _pick_response(messages, int(message["id"]))

    async def _read_capped(self, resp: httpx.Response) -> str:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise McpError("The MCP response is too large", code="too_large")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    # ---- legacy HTTP+SSE transport

    async def _open_sse(self) -> None:
        self._sse_ready = asyncio.Event()
        self._sse_task = asyncio.create_task(self._sse_reader())
        try:
            await asyncio.wait_for(self._sse_ready.wait(), self.config.timeout_seconds)
        except TimeoutError as exc:
            raise McpError(
                "The MCP server did not announce an SSE endpoint", code="timeout"
            ) from exc
        if self._sse_task.done() and self._sse_task.exception() is not None:
            raise McpError("Cannot open the MCP SSE stream", code="unreachable")
        if not self._sse_endpoint:
            raise McpError("The MCP server did not announce an SSE endpoint", code="malformed")

    async def _sse_reader(self) -> None:
        assert self._sse_ready is not None  # noqa: S101
        try:
            async with self._client().stream(
                "GET", self.config.url, headers={"Accept": "text/event-stream"}, timeout=None
            ) as resp:
                if resp.status_code >= 300:
                    raise McpError(f"SSE stream returned HTTP {resp.status_code}")
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    if len(buffer) > MAX_RESPONSE_BYTES:
                        raise McpError("SSE event too large", code="too_large")
                    while "\n\n" in buffer:
                        raw, buffer = buffer.split("\n\n", 1)
                        for event, data in _sse_events(raw + "\n\n"):
                            self._on_sse(event, data)
        finally:
            self._sse_ready.set()
            for fut in self._sse_waiters.values():
                if not fut.done():
                    fut.set_exception(McpError("The MCP SSE stream closed", code="closed"))

    def _on_sse(self, event: str, data: str) -> None:
        assert self._sse_ready is not None  # noqa: S101
        if event == "endpoint":
            endpoint = urljoin(self.config.url, data.strip())
            base, target = urlparse(self.config.url), urlparse(endpoint)
            if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                log.warning("mcp_sse_foreign_endpoint", url=self.config.url)
                return  # never post to another host
            self._sse_endpoint = endpoint
            self._sse_ready.set()
            return
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        for item in msg if isinstance(msg, list) else [msg]:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                fut = self._sse_waiters.get(item["id"])
                if fut is not None and not fut.done() and ("result" in item or "error" in item):
                    fut.set_result(item)

    async def _send_sse(self, message: dict[str, Any]) -> None:
        if not self._sse_endpoint:
            raise McpError("No SSE endpoint", code="malformed")
        try:
            resp = await self._client().post(
                self._sse_endpoint, json=message, headers={"Content-Type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise McpError(
                f"Cannot reach the MCP server: {type(exc).__name__}", code="unreachable"
            ) from exc
        if resp.status_code >= 300:
            raise McpError(
                f"The MCP server returned HTTP {resp.status_code}", code=f"http_{resp.status_code}"
            )

    async def _post_sse(self, message: dict[str, Any]) -> dict[str, Any] | None:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        request_id = int(message["id"])
        self._sse_waiters[request_id] = fut
        try:
            await self._send_sse(message)
            return await asyncio.wait_for(fut, self.config.timeout_seconds)
        except TimeoutError as exc:
            raise McpError("The MCP server did not answer in time", code="timeout") from exc
        finally:
            self._sse_waiters.pop(request_id, None)


@contextlib.asynccontextmanager
async def connect(config: McpConfig) -> AsyncIterator[McpClient]:
    async with McpClient(config) as client:
        yield client


def config_from_integration(
    configuration: dict[str, Any], credentials: dict[str, Any], default_timeout: float
) -> McpConfig:
    from app.integrations.http_api import auth_headers

    return McpConfig(
        url=str(configuration.get("url") or ""),
        transport=configuration.get("mcp_transport") or "auto",
        timeout_seconds=float(configuration.get("timeout_seconds") or default_timeout),
        verify_tls=bool(configuration.get("verify_tls", True)),
        ca_pem=credentials.get("ca") or None,
        headers=auth_headers(configuration, credentials),
    )
