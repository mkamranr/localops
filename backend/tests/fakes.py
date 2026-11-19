"""A fake OpenAI-compatible model server (httpx transport) for tests."""

from __future__ import annotations

import json
import re
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
    # agent_type -> steps; each step is a list of (tool, arguments) calls
    tool_script: dict[str, list[list[tuple[str, dict[str, Any]]]]] = field(default_factory=dict)
    native_tools: bool = True  # False: reject `tools` with HTTP 400 like old servers
    rca: dict[str, Any] | None = None
    rca_statement: str = "The cause is shown by the collected evidence."
    tool_requests: list[dict[str, Any]] = field(default_factory=list)
    # Remediation Planner replies, used in order (the last one repeats)
    remediation_plans: list[dict[str, Any]] = field(default_factory=list)
    planner_prompts: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _reply(
        self, content: str | None, tool_calls: list[dict[str, Any]] | None = None
    ) -> httpx.Response:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            },
        )

    # ---------------------------------------------------------------- tools

    def _agent_of(self, system: str) -> str:
        m = re.search(r"You are the ([A-Za-z ]+?) Agent of LocalOps", system)
        return m.group(1).strip().lower().replace(" ", "_") if m else "unknown"

    def _tool_turn(self, body: dict[str, Any], system: str) -> httpx.Response:
        agent = self._agent_of(system)
        steps = self.tool_script.get(agent, [])
        self.tool_requests.append(body)
        if "tools" in body:
            done = sum(
                1 for m in body["messages"] if m["role"] == "assistant" and m.get("tool_calls")
            )
            if done >= len(steps):
                return self._reply("The evidence is collected.")
            calls = [
                {
                    "id": f"call-{done}-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(steps[done])
            ]
            return self._reply(None, calls)
        # json protocol: one action per reply
        done = sum(1 for m in body["messages"] if m["role"] == "assistant")
        flat = [c for step in steps for c in step]
        if done >= len(flat):
            return self._reply(json.dumps({"final": "The evidence is collected."}))
        name, args = flat[done]
        return self._reply(json.dumps({"tool": name, "arguments": args}))

    @staticmethod
    def _evidence(user: str) -> list[tuple[str, str, bool]]:
        out = []
        for m in re.finditer(
            r"^\[(E\d+\.\d+)\][^\n]*?(\(FAILED\))?\nsummary: ([^\n]*)", user, re.M
        ):
            out.append((m.group(1), m.group(3), not m.group(2)))
        return out

    def _findings(self, user: str) -> str:
        ev = self._evidence(user)
        return json.dumps(
            {
                "summary": f"{len(ev)} evidence items reviewed.",
                "observations": [
                    {
                        "statement": summary,
                        "kind": "confirmed_fact" if ok else "observation",
                        "evidence_ids": [eid],
                    }
                    for eid, summary, ok in ev
                ],
                "hypotheses": [],
            }
        )

    def _rca(self, user: str) -> str:
        if self.rca is not None:
            return json.dumps(self.rca)
        ids = [eid for eid, _, ok in self._evidence(user) if ok]
        return json.dumps(
            {
                "incident_summary": "Summary of the incident.",
                "root_cause": {
                    "statement": self.rca_statement,
                    "status": "confirmed",
                    "confidence": 0.85,
                    "evidence_ids": ids,
                },
                "contributing_factors": [],
                "alternative_hypotheses": [
                    {"statement": "Something else", "likelihood": "low", "evidence_ids": []}
                ],
                "impact": "Users are affected.",
                "recommended_remediation": [
                    {
                        "action": "Apply the fix",
                        "rationale": "Removes the cause",
                        "risk": "low",
                        "evidence_ids": ids[:1],
                    }
                ],
                "preventive_recommendations": ["Add monitoring"],
                "evidence_gaps": [],
            }
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
        user = "\n".join(m["content"] or "" for m in body["messages"] if m["role"] == "user")
        if "calling the read-only diagnostic tools" in system:
            if "tools" in body and not self.native_tools:
                return httpx.Response(400, json={"error": {"message": "tools are not supported"}})
            self.calls.append("tools")
            return self._tool_turn(body, system)
        if "Remediation Planner Agent" in system:
            kind = "remediation"
            self.planner_prompts.append(user)
            plans = self.remediation_plans or [{"steps": [], "notes": "Nothing to run."}]
            idx = min(sum(1 for c in self.calls if c == "remediation"), len(plans) - 1)
            content = json.dumps(plans[idx])
        elif "Classify the operator's request" in system:
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
        elif "turn collected diagnostic evidence into findings" in system:
            kind = "findings"
            content = self._findings(user)
        elif "Root Cause Analysis agent" in system:
            kind = "rca"
            content = self._rca(user)
        else:
            kind = "probe"
            content = "OK"
        self.calls.append(kind)
        if self.before_reply:
            self.before_reply(kind)
        return self._reply(content)
