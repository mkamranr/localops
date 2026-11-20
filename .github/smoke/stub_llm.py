"""Minimal OpenAI-compatible model server for the air-gap smoke test (stdlib only).

Stands in for the customer's own model server inside the simulated air gap:
GET /v1/models, POST /v1/chat/completions (plain and streamed), POST /v1/embeddings
(deterministic bag-of-words vectors, so semantically related text scores higher).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 64
MODELS = ["stub-chat", "stub-embed"]


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        h = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
        vec[h % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/api/v1/health", "/health"):
            self._json(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/embeddings":
            inputs = body.get("input", "")
            items = inputs if isinstance(inputs, list) else [inputs]
            self._json(
                200,
                {
                    "object": "list",
                    "model": body.get("model"),
                    "data": [
                        {"object": "embedding", "index": i, "embedding": embed(str(t))}
                        for i, t in enumerate(items)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        elif self.path == "/v1/chat/completions":
            reply = "Stub model reply: the local model endpoint works."
            created = int(time.time())
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in (({"role": "assistant", "content": reply}, None), ({}, "stop")):
                    chunk = {
                        "id": "stub",
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.get("model"),
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            self._json(
                200,
                {
                    "id": "stub",
                    "object": "chat.completion",
                    "created": created,
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # noqa: S104
