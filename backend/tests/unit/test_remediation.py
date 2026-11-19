"""Phase 5 units: policy engine, remediation commands, approval binding, verification."""

from __future__ import annotations

import shlex
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents import remediation as planner
from app.agents.verification import checks_for, judge
from app.graphs.remediation_nodes import after_approval, after_execution, finalise, next_step
from app.remediation.gateway import action_hash
from app.remediation.policy import DEFAULT_POLICIES, PolicyRule, PolicyRules, evaluate
from app.tools.base import ResourceInfo
from app.tools.registry import TOOLS, change_tools, tools_for_agent
from app.tools.remediation import (
    LinuxProcessArgs,
    LinuxServiceArgs,
    WindowsServiceArgs,
    _linux_kill_script,
    _linux_service_command,
    _windows_service_script,
)


def res(env: str | None = None, rtype: str = "docker_container", **kw: Any) -> ResourceInfo:
    return ResourceInfo(
        id=uuid.uuid4(),
        name="web",
        resource_type=rtype,
        hostname=None,
        ip_addresses=[],
        integration_id=None,
        discovery_target=None,
        facts={},
        health_status="unhealthy",
        environment=env,
        **kw,
    )


def defaults() -> list[PolicyRule]:
    return [
        PolicyRule(p["name"], p["priority"], PolicyRules.model_validate(p["rules"]))
        for p in DEFAULT_POLICIES
    ]


# ------------------------------------------------------------------ policy


def test_default_policies_risk_by_environment() -> None:
    restart = TOOLS["docker_container_restart"]
    dev = evaluate(restart, res("dev"), defaults(), mode="assisted")
    assert dev.risk_level == "low" and dev.auto_approve and dev.requires_approval
    assert "Auto-approval applies only in autonomous mode." in dev.notes
    auto = evaluate(restart, res("dev"), defaults(), mode="autonomous")
    assert auto.requires_approval is False
    prod = evaluate(restart, res("production"), defaults(), mode="autonomous")
    assert prod.risk_level == "high" and prod.requires_approval and not prod.auto_approve
    # an untagged resource may be production: never "non-production" by default
    untagged = evaluate(restart, res(None), defaults(), mode="autonomous")
    assert untagged.risk_level == "medium" and untagged.requires_approval
    unknown = evaluate(TOOLS["docker_container_stop"], res(None), defaults(), mode="autonomous")
    assert unknown.risk_level == "high" and unknown.requires_approval


def test_critical_never_auto_or_self_approved() -> None:
    permissive = [
        *defaults(),
        PolicyRule(
            "yolo",
            100,
            PolicyRules.model_validate(
                {"effect": {"auto_approve": True, "allow_self_approval": True}}
            ),
        ),
    ]
    d = evaluate(TOOLS["linux_reboot"], res("dev", "linux_server"), permissive, mode="autonomous")
    assert d.risk_level == "critical"
    assert d.requires_approval and not d.auto_approve and not d.allow_self_approval
    assert d.approval_ttl_minutes == 30 and len(d.notes) == 2
    # the same permissive rule does let a medium action run unattended
    m = evaluate(
        TOOLS["linux_service_start"], res("dev", "linux_server"), permissive, mode="autonomous"
    )
    assert m.requires_approval is False and m.allow_self_approval


def test_deny_tags_groups_and_disabled_rules() -> None:
    rules = [
        PolicyRule(
            "no db",
            5,
            PolicyRules.model_validate({"match": {"tags": ["Database"]}, "effect": {"deny": True}}),
        ),
        PolicyRule(
            "off",
            6,
            PolicyRules.model_validate({"effect": {"risk_level": "low"}}),
            enabled=False,
        ),
        PolicyRule(
            "team",
            7,
            PolicyRules.model_validate(
                {"match": {"groups": ["payments"]}, "effect": {"approval_ttl_minutes": 15}}
            ),
        ),
    ]
    d = evaluate(TOOLS["docker_container_restart"], res(tags=["database"]), rules, mode="manual")
    assert d.denied and d.denied_reason == "Blocked by policy 'no db'"
    assert d.matched_rules == ["no db"] and d.risk_level == "medium"
    g = evaluate(
        TOOLS["docker_container_restart"], res(group_name="Payments"), rules, mode="manual"
    )
    assert not g.denied and g.approval_ttl_minutes == 15
    with pytest.raises(ValidationError):
        PolicyRules.model_validate({"effect": {"run_anything": True}})


# ------------------------------------------------------------------ tools


def test_change_tools_are_isolated_from_agents() -> None:
    names = {t.name for t in change_tools()}
    assert {"docker_container_restart", "linux_process_terminate", "windows_reboot"} <= names
    for agent in ("infrastructure_diagnostics", "application_diagnostics"):
        assert not {t.name for t in tools_for_agent(agent, sorted(names))}
    for t in change_tools():
        d = t.describe()
        assert d["kind"] == "change" and d["requires_approval"] and t.verify_with


@pytest.mark.parametrize("unit", ["nginx; reboot", "$(id)", "-x", "a b", "nginx`id`", "../etc", ""])
def test_linux_service_rejects_injection(unit: str) -> None:
    with pytest.raises(ValidationError):
        LinuxServiceArgs(resource_id="r", unit=unit)


def test_linux_service_command_is_quoted() -> None:
    cmd = _linux_service_command("restart", LinuxServiceArgs(resource_id="r", unit="nginx").unit)
    assert cmd.startswith("sudo -n systemctl restart -- nginx 2>&1;")
    assert "systemctl is-active -- nginx" in cmd


def test_windows_service_name_is_literal() -> None:
    with pytest.raises(ValidationError):
        WindowsServiceArgs(resource_id="r", name="Spooler'; Restart-Computer; '")
    script = _windows_service_script("restart", "Print Spooler")
    assert "$n = 'Print Spooler'" in script and "Restart-Service -Name $svc.Name -Force" in script


def test_process_termination_guards() -> None:
    for pid in (0, 1, -5):
        with pytest.raises(ValidationError):
            LinuxProcessArgs(resource_id="r", pid=pid, expected_command="python")
    with pytest.raises(ValidationError):
        LinuxProcessArgs(resource_id="r", pid=10, expected_command="py\nrm -rf /")
    args = LinuxProcessArgs(
        resource_id="r", pid=4242, expected_command="worker.py'; reboot; '", force=True
    )
    script = _linux_kill_script(args)
    assert f"*{shlex.quote(args.expected_command)}*" in script
    assert "kill -s TERM -- 4242" in script and "kill -s KILL -- 4242" in script
    assert "sudo" not in script
    # the command-line check comes before any signal
    assert script.index("MISMATCH") < script.index("kill -s TERM")


# ------------------------------------------------------------------ approval binding


def test_action_hash_binds_every_field() -> None:
    rid, inv = uuid.uuid4(), uuid.uuid4()
    base = action_hash("docker_container_restart", {"resource_id": str(rid), "t": 10}, rid, inv)
    assert base == action_hash(
        "docker_container_restart", {"t": 10, "resource_id": str(rid)}, rid, inv
    )
    variants = [
        action_hash("docker_container_stop", {"resource_id": str(rid), "t": 10}, rid, inv),
        action_hash("docker_container_restart", {"resource_id": str(rid), "t": 11}, rid, inv),
        action_hash(
            "docker_container_restart", {"resource_id": str(rid), "t": 10}, uuid.uuid4(), inv
        ),
        action_hash(
            "docker_container_restart", {"resource_id": str(rid), "t": 10}, rid, uuid.uuid4()
        ),
    ]
    assert base not in variants and len(set(variants)) == 4


# ------------------------------------------------------------------ verification


def ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": "", "data": data}


def test_judge_services_and_containers() -> None:
    done = {"status": "succeeded"}
    svc = judge(
        "linux_service_restart",
        {"unit": "nginx"},
        done,
        {"linux_service_status": ok({"unit": {"ActiveState": "active", "SubState": "running"}})},
    )
    assert svc.status == "passed"
    stop = judge(
        "linux_service_stop",
        {"unit": "nginx"},
        done,
        {"linux_service_status": ok({"unit": {"ActiveState": "active"}})},
    )
    assert stop.status == "failed"
    win = judge(
        "windows_service_restart",
        {"name": "Spooler"},
        done,
        {"windows_service_status": ok({"service": {"state": "Running"}})},
    )
    assert win.status == "passed"

    def state(**st: Any) -> dict[str, Any]:
        return {"docker_container_state": ok({"state": st})}

    healthy = judge(
        "docker_container_restart",
        {},
        done,
        state(status="running", running=True, restart_count=15),
        state(status="restarting", running=False, restart_count=14),
    )
    assert healthy.status == "passed"
    loop = judge(
        "docker_container_restart",
        {},
        done,
        state(status="running", running=True, restart_count=18),
        state(status="restarting", running=False, restart_count=14),
    )
    assert loop.status == "failed" and "crash loop" in loop.summary
    unhealthy = judge(
        "docker_container_start",
        {},
        done,
        state(status="running", running=True, health_status="unhealthy"),
    )
    assert unhealthy.status == "failed"
    failed = judge("docker_container_restart", {}, {"status": "failed", "summary": "x"}, {})
    assert failed.status == "failed"


def test_judge_process_and_checks() -> None:
    procs = {"linux_top_processes": ok({"processes": [{"pid": 880}, {"pid": 4242}]})}
    gone = {"status": "succeeded", "data": {"terminated": True}}
    assert judge("linux_process_terminate", {"pid": 4242}, gone, procs).status == "failed"
    assert judge("linux_process_terminate", {"pid": 4243}, gone, procs).status == "passed"
    checks = checks_for(
        "linux_service_restart", ("linux_service_status",), {"resource_id": "r", "unit": "x"}
    )
    assert [(c.tool, c.arguments) for c in checks] == [
        ("linux_service_status", {"resource_id": "r", "unit": "x"})
    ]


# ------------------------------------------------------------------ planner


def test_planner_validation() -> None:
    assert planner.can_plan({}) is not None
    assert planner.can_plan({"root_cause": {"status": "unverified"}}) is not None
    assert planner.can_plan({"root_cause": {"status": "probable"}}) is None
    step = planner.PlanStep.model_validate(
        {"action": "docker_container_restart", "args": {"resource_id": "x"}, "evidence": "E1.2, E9"}
    )
    assert step.tool == "docker_container_restart" and step.evidence_ids == ["E1.2", "E9"]
    assert planner.check_citations(step, {"E1.2"}) is None and step.evidence_ids == ["E1.2"]
    bare = planner.PlanStep(tool="docker_container_restart")
    assert planner.check_citations(bare, {"E1.2"})
    tools = planner.applicable_tools(
        change_tools(), [{"resolved": True, "resource_type": "docker_container"}]
    )
    assert {t.name for t in tools} == {
        "docker_container_restart",
        "docker_container_start",
        "docker_container_stop",
    }
    msgs = planner.plan_messages(
        "fix it",
        {"root_cause": {"statement": "s", "status": "confirmed", "evidence_ids": ["E1.1"]}},
        [{"id": "E1.1", "tool": "docker_container_state", "summary": "restarting"}],
        [{"resolved": True, "resource_id": "abc", "name": "web", "resource_type": "x"}],
        tools,
        max_steps=2,
        feedback="use a longer timeout",
        previous={"tool": "docker_container_restart"},
    )
    assert "At most 2 steps" in msgs[0].content
    assert "<comment>use a longer timeout</comment>" in msgs[1].content
    assert "Remediation Planner Agent" in msgs[0].content


def test_routing_and_final_status() -> None:
    def st(*statuses: str, status: str = "planned") -> dict[str, Any]:
        return {
            "remediation": {
                "status": status,
                "steps": [{"index": i, "status": s} for i, s in enumerate(statuses)],
            }
        }

    assert next_step(st("proposed")) == "request_approval"
    assert next_step(st("proposed", status="advisory")) == "report"
    assert next_step({}) == "report"
    assert after_approval(st("approved")) == "execute_remediation"
    assert after_approval(st("changes_requested")) == "replan_step"
    assert after_approval(st("rejected", "proposed")) == "request_approval"
    assert after_execution(st("executed")) == "verify_remediation"
    assert after_execution(st("failed", status="stopped")) == "report"
    assert finalise(st("verified", "rejected")["remediation"])["status"] == "verified"
    assert finalise(st("unverified")["remediation"])["status"] == "unverified"
    assert finalise(st("rejected")["remediation"])["status"] == "not_executed"
    assert finalise(None) is None
