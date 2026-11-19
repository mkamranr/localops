"""Read-only diagnostic tools: validation, command construction, redaction."""

from __future__ import annotations

import json
import struct
import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.base import Target
from app.integrations.ssh import CommandResult
from app.tools import docker as docker_tools
from app.tools import linux, windows
from app.tools.base import ResourceInfo
from app.tools.context import ToolContext
from app.tools.redaction import cap, redact
from app.tools.registry import AGENT_TOOLS, TOOLS, tools_for_agent
from app.tools.transport import Connection, set_transport_factory


class FakeTransport:
    def __init__(self, replies: dict[str, str] | None = None, ps: Any = None) -> None:
        self.commands: list[str] = []
        self.scripts: list[str] = []
        self.replies = replies or {}
        self.ps = ps

    async def shell(self, command: str) -> CommandResult:
        self.commands.append(command)
        for prefix, out in self.replies.items():
            if command.startswith(prefix):
                return CommandResult(0, out, "")
        return CommandResult(0, "", "")

    async def powershell(self, script: str) -> str:
        self.scripts.append(script)
        return json.dumps(self.ps)

    async def docker_get(self, path: str) -> httpx.Response:
        raise AssertionError("not used")


@pytest.fixture
def fake():
    transport = FakeTransport()
    set_transport_factory(lambda conn, timeout: transport)
    yield transport
    set_transport_factory(None)


def ctx_for(resource_type: str = "linux_server", container: str | None = None) -> ToolContext:
    resource = ResourceInfo(
        id=uuid.uuid4(),
        name="app-01",
        resource_type=resource_type,
        hostname="app-01",
        ip_addresses=["10.0.0.11"],
        integration_id=uuid.uuid4(),
        discovery_target="10.0.0.11",
        facts={},
        health_status="healthy",
    )
    ctx = ToolContext(
        settings=Settings(app_secret_key="x" * 40),  # type: ignore[call-arg]
        tenant_id=uuid.uuid4(),
        resource=resource,
        timeout=10,
    )
    ctx._connection = Connection(
        kind="ssh",
        target=Target("10.0.0.11"),
        config={},
        credentials={},
        pinned_key="ssh-ed25519 AAAA",
        container=container,
    )
    return ctx


RID = str(uuid.uuid4())


@pytest.mark.parametrize(
    "unit",
    ["nginx; rm -rf /", "$(reboot)", "`id`", "-H evil", "a b", "x|y", "../etc", "n" * 101, ""],
)
def test_unit_names_reject_injection(unit: str) -> None:
    with pytest.raises(ValidationError):
        linux.ServiceArgs(resource_id=RID, unit=unit)


def test_arguments_are_bounded_and_strict() -> None:
    with pytest.raises(ValidationError):
        linux.JournalArgs(resource_id=RID, lines=10_000)
    with pytest.raises(ValidationError):
        linux.JournalArgs(resource_id=RID, priority="everything")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):  # unknown fields are refused
        linux.TopProcessesArgs.model_validate({"resource_id": RID, "command": "ls"})
    with pytest.raises(ValidationError):
        windows.ServiceArgs(resource_id=RID, name="Spooler'; Stop-Computer; '")
    with pytest.raises(ValidationError):
        windows.EventLogArgs(resource_id=RID, contains="$(Remove-Item C:\\)")


async def test_service_status_quotes_the_unit(fake: FakeTransport) -> None:
    fake.replies["systemctl show"] = (
        "Id=nginx.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\n"
        "Result=exit-code\nNRestarts=5\n"
    )
    out = await linux.service_status(ctx_for(), linux.ServiceArgs(resource_id=RID, unit="nginx"))
    assert "-- nginx;" in fake.commands[0]
    assert out.summary == "nginx.service: failed/failed, result exit-code, restarts 5"


async def test_journal_command_and_permission_hint(fake: FakeTransport) -> None:
    args = linux.JournalArgs(resource_id=RID, unit="app@1.service", priority="err", lines=50)
    fake.replies["if command -v journalctl"] = "2026-11-19T09:00:00 app[1]: boom\n"
    out = await linux.journal(ctx_for(), args)
    cmd = fake.commands[0]
    assert "-p err -n 50 -u app@1.service" in cmd and "'60 min ago'" in cmd
    assert out.data["entries"] == 1
    fake.replies["if command -v journalctl"] = (
        "No journal files were opened due to insufficient permissions."
    )
    with pytest.raises(Exception, match="systemd-journal"):
        await linux.journal(ctx_for(), args)


async def test_top_processes_summary(fake: FakeTransport) -> None:
    fake.replies["ps -eo"] = (
        "  PID  PPID USER %CPU %MEM RSS ELAPSED STAT COMMAND\n"
        " 4242 1 app 97.5 1.2 12000 01:02:03 R python worker.py --loop\n"
    )
    out = await linux.top_processes(ctx_for(), linux.TopProcessesArgs(resource_id=RID))
    assert "--sort=-%cpu" in fake.commands[0] and "head -n 16" in fake.commands[0]
    assert out.summary == "Top process by cpu: python worker.py --loop (97.5% CPU, 1.2% memory)"


async def test_container_tools_quote_names(fake: FakeTransport) -> None:
    inspect = [
        {
            "Name": "/web",
            "RestartCount": 14,
            "State": {"Status": "restarting", "ExitCode": 1, "OOMKilled": False},
            "Config": {"Image": "shop/web:3", "Env": ["DATABASE_URL=postgres://u:p@db/x", "A=1"]},
            "HostConfig": {"RestartPolicy": {"Name": "always"}},
        }
    ]
    fake.replies["docker inspect"] = json.dumps(inspect)
    ctx = ctx_for("docker_container", container="web")
    out = await docker_tools.container_state(ctx, docker_tools.ResourceArgs(resource_id=RID))
    assert fake.commands[0] == "docker inspect --type container -- web"
    assert out.summary == "web: restarting, exit code 1, 14 restarts"
    # environment values never leave the host side
    assert "postgres://" not in out.text and "DATABASE_URL" in out.text
    fake.replies["docker logs"] = "2026 Error: DATABASE_URL is not set\n"
    logs = await docker_tools.container_logs(ctx, docker_tools.LogsArgs(resource_id=RID, tail=50))
    assert fake.commands[1] == "docker logs --timestamps --tail 50 -- web 2>&1"
    assert logs.data == {"lines": 1, "errors": 1}


async def test_bad_container_name_is_refused(fake: FakeTransport) -> None:
    ctx = ctx_for("docker_container", container="web; reboot")
    with pytest.raises(Exception, match="not valid"):
        await docker_tools.container_state(ctx, docker_tools.ResourceArgs(resource_id=RID))
    assert fake.commands == []


async def test_windows_event_log_script(fake: FakeTransport) -> None:
    fake.ps = [{"time": "t", "id": 7034, "level": "Error", "provider": "SCM", "message": "x"}]
    args = windows.EventLogArgs(resource_id=RID, contains="Print Spooler", since_minutes=60)
    out = await windows.event_log(ctx_for("windows_server"), args)
    script = fake.scripts[0]
    assert "LogName = 'System'; Level = 1,2" in script
    assert "('*' + 'Print Spooler' + '*')" in script
    assert out.summary.endswith("(ids 7034)")


def test_ps_literal_escapes_quotes() -> None:
    assert windows.ps_literal("it's") == "'it''s'"


def test_docker_log_demux() -> None:
    frame = b"\x01\x00\x00\x00" + struct.pack(">I", 6) + b"hello\n"
    frame += b"\x02\x00\x00\x00" + struct.pack(">I", 4) + b"err\n"
    assert docker_tools.demux(frame) == "hello\nerr\n"
    assert docker_tools.demux(b"plain tty output") == "plain tty output"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("DB_PASSWORD=hunter2", "hunter2"),
        ("mysqld --password s3cr3t --user root", "s3cr3t"),
        ("postgres://app:pw123@db:5432/x", "pw123"),
        ("Authorization: Bearer abc.def.ghi123", "abc.def"),
        ('{"api_key": "XYZ123456"}', "XYZ123456"),
        ("key AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEsecret\n-----END RSA PRIVATE KEY-----", "MIIE"),
    ],
)
def test_redaction_masks_secrets(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_redaction_keeps_prose() -> None:
    for text in ("Error: DATABASE_URL is not set", "password: is required", "token=expired"):
        assert redact(text) == text


def test_cap_keeps_head_and_tail() -> None:
    capped = cap("A" * 5000 + "B" * 5000, 1000)
    assert capped.startswith("A") and capped.endswith("B") and "omitted" in capped
    assert len(capped.encode()) < 1100


def test_catalogue_is_consistent() -> None:
    for agent, names in AGENT_TOOLS.items():
        assert all(n in TOOLS for n in names), agent
    for spec in TOOLS.values():
        schema = spec.openai_tool()["function"]["parameters"]
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        if spec.is_change:
            assert spec.describe()["requires_approval"] is True
            assert spec.permission == "tool.execute.remediation"
        else:
            assert spec.describe()["risk_level"] == "read"
    assert [t.name for t in tools_for_agent("network_diagnostics", ["net_tcp_check", "nope"])] == [
        "net_tcp_check"
    ]
