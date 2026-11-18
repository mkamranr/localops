"""A fake OpenAI-compatible model server (httpx transport) for tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

CLASSIFICATION = {
    "request_type": "incident_investigation",
    "summary": "Payroll API is slow for users since this morning.",
    "affected_entities": ["payroll-api", "db-prod-01"],
    "symptoms": ["high latency"],
    "urgency": "high",
    "needs_clarification": False,
    "clarification_question": None,
    "confidence": 0.82,
}

VAGUE = {
    **CLASSIFICATION,
    "request_type": "general_question",
    "affected_entities": [],
    "needs_clarification": True,
    "clarification_question": "Which system is affected?",
    "confidence": 0.3,
}

PLAN = {
    "tasks": [
        {
            "agent": "infrastructure_diagnostics",
            "objective": "Check CPU, memory and disk on db-prod-01",
            "rationale": "Database saturation commonly causes API latency",
        },
        {
            "agent": "Application Diagnostics Agent",
            "objective": "Inspect payroll-api error rate and latency",
            "rationale": "Confirm the symptom",
        },
        {"agent": "made_up_agent", "objective": "Should be dropped", "rationale": ""},
    ]
}


class SimulatedCrashError(BaseException):
    """Escapes ``except Exception`` like a killed process would."""


@dataclass
class FakeLLM:
    """Routes requests by the supervisor prompt they contain."""

    classifications: list[dict[str, Any]] = field(default_factory=lambda: [CLASSIFICATION])
    plan: dict[str, Any] = field(default_factory=lambda: PLAN)
    models: list[str] = field(default_factory=lambda: ["qwen2.5:7b"])
    fail_status: int | None = None
    crash_on_plan: bool = False
    calls: list[str] = field(default_factory=list)
    auth_headers: list[str | None] = field(default_factory=list)
    before_reply: Callable[[str], None] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _reply(self, content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.auth_headers.append(request.headers.get("authorization"))
        path = request.url.path
        if path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if not path.endswith("/v1/chat/completions"):
            return httpx.Response(404, json={"error": "not found"})
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "boom"}})
        body = json.loads(request.content)
        roles = [m["role"] for m in body["messages"]]
        if "system" in roles[1:]:
            # Behave like strict chat templates (Mistral, Gemma, some vLLM setups).
            return httpx.Response(
                400, json={"error": {"message": "System message must be at the beginning."}}
            )
        system = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")
        if "Classify the operator's request" in system:
            kind = "classify"
            idx = min(sum(1 for c in self.calls if c == "classify"), len(self.classifications) - 1)
            content = json.dumps(self.classifications[idx])
        elif "investigation plan" in system:
            kind = "plan"
            if self.crash_on_plan:
                self.crash_on_plan = False
                self.calls.append("plan-crash")
                raise SimulatedCrashError
            content = "<think>planning…</think>```json\n" + json.dumps(self.plan) + "\n```"
        else:
            kind = "probe"
            content = "OK"
        self.calls.append(kind)
        if self.before_reply:
            self.before_reply(kind)
        return self._reply(content)
