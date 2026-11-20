"""Verification agent: decides, deterministically, whether a remediation worked.

It re-runs the change tool's read-only ``verify_with`` tools after the action and
compares with the state before. The verdict comes from fixed rules per tool; no model
is involved, so a model can never declare success that the systems do not show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.tools.remediation import VERIFY_ARGS


@dataclass(slots=True)
class Check:
    tool: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Verdict:
    status: str  # passed | failed
    summary: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "reasons": self.reasons}


def checks_for(tool: str, verify_with: tuple[str, ...], arguments: dict[str, Any]) -> list[Check]:
    out = []
    for name in verify_with:
        args: dict[str, Any] = {"resource_id": arguments.get("resource_id")}
        for key in VERIFY_ARGS.get(name, ()):
            if key in arguments:
                args[key] = arguments[key]
        if name == "linux_top_processes":
            args["limit"] = 25
        out.append(Check(name, args))
    return out


def _operation(tool: str) -> str:
    return tool.rsplit("_", 1)[-1]


def judge(
    tool: str,
    arguments: dict[str, Any],
    execution: dict[str, Any],
    after: dict[str, dict[str, Any]],
    before: dict[str, dict[str, Any]] | None = None,
) -> Verdict:
    """``after``/``before``: verify tool name → {"ok", "summary", "data"}."""
    before = before or {}
    if execution.get("status") != "succeeded":
        return Verdict("failed", "The action did not complete", [str(execution.get("summary"))])
    op = _operation(tool)
    reasons: list[str] = []

    if tool.startswith("linux_service_"):
        unit = (after.get("linux_service_status", {}).get("data") or {}).get("unit") or {}
        state = unit.get("ActiveState")
        reasons.append(f"{arguments.get('unit')} is {state or 'unknown'}/{unit.get('SubState')}")
        ok = state == "inactive" if op == "stop" else state == "active"
        return _verdict(ok, tool, reasons)

    if tool.startswith("windows_service_"):
        svc = (after.get("windows_service_status", {}).get("data") or {}).get("service") or {}
        state = str(svc.get("state") or "")
        reasons.append(f"{arguments.get('name')} is {state or 'unknown'}")
        ok = state == "Stopped" if op == "stop" else state == "Running"
        return _verdict(ok, tool, reasons)

    if tool.startswith("docker_container_"):
        st = (after.get("docker_container_state", {}).get("data") or {}).get("state") or {}
        prev = (before.get("docker_container_state", {}).get("data") or {}).get("state") or {}
        reasons.append(
            f"container is {st.get('status') or 'unknown'}, restart count {st.get('restart_count')}"
        )
        if op == "stop":
            return _verdict(
                st.get("status") == "exited" or st.get("running") is False, tool, reasons
            )
        ok = bool(st.get("running")) and not st.get("restarting")
        if st.get("health_status") == "unhealthy":
            ok = False
            reasons.append("health check reports unhealthy")
        before_restarts = prev.get("restart_count")
        if (
            ok
            and isinstance(before_restarts, int)
            and isinstance(st.get("restart_count"), int)
            and st["restart_count"] > before_restarts + (1 if op == "restart" else 0)
        ):
            ok = False
            reasons.append("the restart count keeps rising (crash loop)")
        return _verdict(ok, tool, reasons)

    if tool.endswith("_process_terminate"):
        pid = arguments.get("pid")
        key = "linux_top_processes" if tool.startswith("linux") else "windows_top_processes"
        procs = (after.get(key, {}).get("data") or {}).get("processes") or []
        pids = {int(p.get("pid") or p.get("Id") or p.get("id") or -1) for p in procs}
        still = pid in pids
        terminated = bool((execution.get("data") or {}).get("terminated"))
        reasons.append(
            f"process {pid} is still listed"
            if still
            else f"process {pid} is no longer among the top processes"
        )
        return _verdict(terminated and not still, tool, reasons)

    if tool.endswith("_reboot"):
        snap = next(iter(after.values()), {})
        reasons.append(
            "host answered after the reboot" if snap.get("ok") else "host not reachable yet"
        )
        return _verdict(bool(snap.get("ok")), tool, reasons)

    if not after:
        # Nothing re-checked the system: never report success that nobody observed.
        return Verdict(
            "failed",
            "Not verified: no verification tool is configured for this action; "
            "verify the result manually",
            ["no verification tool configured"],
        )
    ok = all(v.get("ok") for v in after.values())
    reasons += [f"{name}: {v.get('summary')}" for name, v in after.items()]
    return _verdict(ok, tool, reasons or ["verification tools ran"])


def _verdict(ok: bool, tool: str, reasons: list[str]) -> Verdict:
    if ok:
        return Verdict("passed", f"Verified: {'; '.join(reasons)}", reasons)
    return Verdict("failed", f"Not verified: {'; '.join(reasons)}", reasons)
