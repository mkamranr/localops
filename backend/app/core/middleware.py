"""ASGI middleware: request IDs, Prometheus metrics, security headers."""

from __future__ import annotations

import re
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger, request_id_var

log = get_logger("localops.http")

HTTP_REQUESTS = Counter(
    "localops_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "localops_http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)

_VALID_RID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


class RequestContextMiddleware:
    """Assigns a request ID, records metrics and emits one structured log per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _VALID_RID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        status = 500
        start = time.perf_counter()

        async def _send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                headers.extend(_SECURITY_HEADERS)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            elapsed = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_LATENCY.labels(method, route).observe(elapsed)
            if route not in ("/api/v1/health", "/metrics"):
                log.info(
                    "http_request",
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 1),
                )
            request_id_var.reset(token)
