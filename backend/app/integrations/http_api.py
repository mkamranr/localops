"""Custom HTTP tools: admin-defined request templates against one ``http_api`` base URL.

A template fixes the method, path, query, headers and JSON body; only ``{placeholder}``
values come from the (schema-validated) tool arguments, and they are substituted
safely:

- path placeholders fill one path segment each: values are percent-encoded and may
  not contain ``/`` or ``\\`` or be ``.``/``..``;
- the final URL must keep the base URL's scheme, host, port and path prefix (no SSRF);
- header values may not contain CR/LF;
- the body is a JSON structure: a string that is exactly ``"{name}"`` becomes the typed
  argument value, other strings get text substitution — never string-built JSON;
- redirects are not followed, proxies are ignored, the response size is capped.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.tools.base import ToolError

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,63})\}")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}
DEFAULT_MAX_BYTES = 200_000

# Tests inject an httpx transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class TemplateError(ValueError):
    """The template (or its rendering with given arguments) is not allowed."""


@dataclass(frozen=True, slots=True)
class RenderedRequest:
    method: str
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    body: Any  # JSON value or None


def auth_headers(configuration: dict[str, Any], credentials: dict[str, Any]) -> dict[str, str]:
    """Authorization headers for MCP and HTTP API integrations."""
    auth = configuration.get("auth_type") or "none"
    token = str(credentials.get("token") or "")
    if auth == "bearer" and token:
        return {"Authorization": f"Bearer {token}"}
    if auth == "header" and token and configuration.get("auth_header"):
        return {str(configuration["auth_header"]): token}
    if auth == "basic" and configuration.get("username"):
        raw = f"{configuration['username']}:{credentials.get('password') or ''}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {}


def verify_option(
    configuration: dict[str, Any], credentials: dict[str, Any]
) -> ssl.SSLContext | bool:
    if not configuration.get("verify_tls", True):
        return False
    if credentials.get("ca"):
        return ssl.create_default_context(cadata=str(credentials["ca"]))
    return True


def placeholders(template: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(PLACEHOLDER.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for key in ("path", "query", "headers", "body"):
        walk(template.get(key))
    return found


def check_template(template: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Validate a template against its argument schema; returns the normalised form."""
    method = str(template.get("method") or "GET").upper()
    if method not in METHODS:
        raise TemplateError(f"method must be one of {', '.join(METHODS)}")
    path = str(template.get("path") or "/")
    if not path.startswith("/") or "://" in path or "\\" in path or ".." in path.split("/"):
        raise TemplateError("path must be a relative path starting with '/'")
    if "?" in path or "#" in path:
        raise TemplateError("put query parameters in 'query', not in the path")
    query = template.get("query") or {}
    headers = template.get("headers") or {}
    if not isinstance(query, dict) or not all(isinstance(v, str) for v in query.values()):
        raise TemplateError("query must map parameter names to string templates")
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise TemplateError("headers must map header names to string templates")
    for name, value in headers.items():
        if not HEADER_NAME.match(name) or name.lower() in FORBIDDEN_HEADERS:
            raise TemplateError(f"header {name!r} is not allowed")
        if "\r" in value or "\n" in value:
            raise TemplateError(f"header {name!r} contains a line break")
    if template.get("body") is not None and method in ("GET", "DELETE"):
        raise TemplateError(f"{method} requests cannot have a body")
    props = set((schema.get("properties") or {}).keys())
    unknown = placeholders(template) - props
    if unknown:
        raise TemplateError(
            f"placeholders without a matching parameter: {', '.join(sorted(unknown))}"
        )
    max_bytes = int(template.get("response_max_bytes") or DEFAULT_MAX_BYTES)
    return {
        "method": method,
        "path": path,
        "query": dict(query),
        "headers": dict(headers),
        "body": template.get("body"),
        "response_max_bytes": max(1000, min(max_bytes, 2_000_000)),
    }


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _segment(value: Any, name: str) -> str:
    text = _text(value)
    if not text or text in (".", "..") or "/" in text or "\\" in text:
        raise TemplateError(f"'{name}' is not a valid path segment")
    return quote(text, safe="")


def render(base_url: str, template: dict[str, Any], args: dict[str, Any]) -> RenderedRequest:
    base = urlparse(base_url)
    if base.scheme not in ("http", "https") or not base.netloc:
        raise TemplateError("the integration base URL must be http(s)")

    def fill(text: str, *, path: bool = False, required: bool = True) -> str | None:
        missing = [n for n in PLACEHOLDER.findall(text) if args.get(n) is None]
        if missing:
            if required:
                raise TemplateError(f"missing argument(s): {', '.join(missing)}")
            return None
        return PLACEHOLDER.sub(
            lambda m: _segment(args[m.group(1)], m.group(1)) if path else _text(args[m.group(1)]),
            text,
        )

    path = fill(template["path"], path=True) or "/"
    url = base_url.rstrip("/") + path
    final = urlparse(url)
    prefix = base.path.rstrip("/")
    if (final.scheme, final.netloc) != (base.scheme, base.netloc) or not final.path.startswith(
        prefix
    ):
        raise TemplateError("the request would leave the integration's base URL")
    params: dict[str, str] = {}
    for key, value in (template.get("query") or {}).items():
        filled = fill(value, required=False)  # optional arguments drop their parameter
        if filled is not None:
            params[key] = filled
    headers: dict[str, str] = {}
    for key, value in (template.get("headers") or {}).items():
        filled = fill(value, required=False)
        if filled is None:
            continue
        if "\r" in filled or "\n" in filled:
            raise TemplateError(f"header {key!r} would contain a line break")
        headers[key] = filled

    def body(value: Any) -> Any:
        if isinstance(value, str):
            whole = PLACEHOLDER.fullmatch(value)
            if whole:
                return args.get(whole.group(1))
            return fill(value, required=False) or ""
        if isinstance(value, dict):
            return {k: body(v) for k, v in value.items()}
        if isinstance(value, list):
            return [body(v) for v in value]
        return value

    raw_body = template.get("body")
    return RenderedRequest(
        method=template["method"],
        url=url,
        params=params,
        headers=headers,
        body=body(raw_body) if raw_body is not None else None,
    )


async def execute(
    configuration: dict[str, Any],
    credentials: dict[str, Any],
    template: dict[str, Any],
    args: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[str, str, dict[str, Any]]:
    """Run the request; returns (model text, summary, data). Raises ``ToolError``."""
    try:
        req = render(str(configuration.get("url") or ""), template, args)
    except TemplateError as exc:
        raise ToolError(str(exc), code="invalid_arguments") from exc
    max_bytes = int(template.get("response_max_bytes") or DEFAULT_MAX_BYTES)
    try:
        async with (
            httpx.AsyncClient(
                transport=_transport_override,
                timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
                verify=verify_option(configuration, credentials),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "LocalOps-Tools/0.6", "Accept": "application/json, */*"},
            ) as client,
            client.stream(
                req.method,
                req.url,
                params=req.params or None,
                headers={**req.headers, **auth_headers(configuration, credentials)},
                json=req.body if req.body is not None else None,
            ) as resp,
        ):
            chunks: list[bytes] = []
            size = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    truncated = True
                    break
                chunks.append(chunk)
            status = resp.status_code
            ctype = resp.headers.get("content-type", "")
    except httpx.TimeoutException as exc:
        raise ToolError("The API did not answer in time", code="timeout") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Cannot reach the API: {type(exc).__name__}", code="unreachable") from exc
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if "json" in ctype and not truncated:
        with contextlib.suppress(json.JSONDecodeError):
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
    if truncated:
        text += f"\n… (response truncated at {max_bytes} bytes)"
    if 300 <= status < 400:
        raise ToolError(
            f"The API redirected (HTTP {status}); redirects are not followed", code="redirect"
        )
    if status >= 400:
        raise ToolError(f"The API returned HTTP {status}: {text[:300]}", code=f"http_{status}")
    path = urlparse(req.url).path
    return (
        f"HTTP {status} {req.method} {path}\n{text}",
        f"{req.method} {path} → HTTP {status}",
        {"status": status},
    )
