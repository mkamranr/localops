"""Realistic collector outputs and an in-process SSH server for discovery tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import asyncssh

MACHINE_ID = "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f"

LINUX_OUTPUT = f"""@@hostname
app-01
@@fqdn
app-01.corp.local
@@machine_id
{MACHINE_ID}
@@os_release
NAME="Ubuntu"
VERSION_ID="24.04"
ID=ubuntu
PRETTY_NAME="Ubuntu 24.04.1 LTS"
@@kernel
Linux 6.8.0-45-generic x86_64
@@uptime
86400.52 170000.10
@@loadavg
0.42 0.35 0.30 1/312 4242
@@nproc
4
@@meminfo
MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    2000000 kB
@@df
Filesystem     Type 1024-blocks     Used Available Capacity Mounted on
/dev/sda1      ext4    41152736 38000000   3152736      93% /
/dev/sdb1      xfs    104857600 10485760  94371840      10% /data files
@@ip
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.0.11/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever
2: eth0    inet6 fe80::1/64 scope link \\       valid_lft forever preferred_lft forever
@@services
ssh.service          loaded active running OpenBSD Secure Shell server
nginx.service        loaded active running A high performance web server
docker.service       loaded active running Docker Application Container Engine
@@failed
backup.service       loaded failed failed  Nightly backup
@@listen
LISTEN 0      4096         0.0.0.0:22        0.0.0.0:*
LISTEN 0      511          0.0.0.0:80        0.0.0.0:*
LISTEN 0      4096            [::]:22           [::]:*
LISTEN 0      4096         0.0.0.0:8080      0.0.0.0:*
@@established
0      0           10.0.0.11:22         10.0.0.99:51514
0      0           10.0.0.11:43210      10.0.0.12:5432
0      0           10.0.0.11:43212      10.0.0.12:5432
0      0      [::ffff:10.0.0.11]:80  [::ffff:10.0.0.50]:40000
0      0           10.0.0.11:50000      127.0.0.1:6379
@@docker
present
@@end
"""

DOCKER_INFO = {
    "ID": "ENGINE-1",
    "Name": "app-01",
    "ServerVersion": "27.3.1",
    "Containers": 2,
    "ContainersRunning": 1,
    "ContainersStopped": 1,
    "Images": 5,
    "Driver": "overlay2",
}


def container(
    name: str,
    *,
    state: str = "running",
    health: str | None = None,
    exit_code: int = 0,
    ports: dict[str, Any] | None = None,
    image: str = "nginx:1.27",
    restarts: int = 0,
) -> dict[str, Any]:
    return {
        "Id": (name.encode().hex() * 8)[:64],
        "Name": f"/{name}",
        "RestartCount": restarts,
        "Config": {
            "Image": image,
            "Labels": {
                "com.docker.compose.project": "shop",
                "com.docker.compose.service": name,
            },
        },
        "State": {
            "Status": state,
            "ExitCode": exit_code,
            "StartedAt": "2026-11-18T08:00:00Z",
            **({"Health": {"Status": health}} if health else {}),
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {
            "Ports": ports or {},
            "Networks": {"shop_default": {"IPAddress": "172.18.0.2"}},
        },
        "Mounts": [{"Type": "volume"}],
    }


CONTAINERS = [
    container(
        "web",
        health="healthy",
        ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]},
    ),
    container("migrate", state="exited", exit_code=1, image="shop/migrate:2"),
]

DB_LINUX_OUTPUT = (
    LINUX_OUTPUT.replace("app-01", "db-01")
    .replace(MACHINE_ID, "a" * 32)
    .replace("10.0.0.11", "10.0.0.12")
    .replace("93% /", "40% /")
    .replace("@@docker\npresent\n", "@@docker\n")
)

WINDOWS_JSON = {
    "hostname": "WIN-FS01",
    "domain": "corp.local",
    "uuid": "4C4C4544-0042-3510-8051-B4C04F4E4D32",
    "os": "Microsoft Windows Server 2022 Standard",
    "version": "10.0.20348",
    "build": "20348",
    "last_boot": "2026-11-17T06:00:00.0000000Z",
    "total_memory_kb": 16000000,
    "free_memory_kb": 8000000,
    "cpu_count": 8,
    "cpu_percent": 25,
    "disks": [
        {"DeviceID": "C:", "FileSystem": "NTFS", "Size": 100 * 1024**3, "FreeSpace": 5 * 1024**3}
    ],
    "services": [
        {"Name": "W32Time", "DisplayName": "Windows Time", "State": "Running", "StartMode": "Auto"},
        {
            "Name": "Spooler",
            "DisplayName": "Print Spooler",
            "State": "Stopped",
            "StartMode": "Auto",
        },
    ],
    "ips": ["10.0.0.20", "127.0.0.1"],
    "listen": [{"LocalAddress": "0.0.0.0", "LocalPort": 445}],
    "established": [
        {"LocalPort": 445, "RemoteAddress": "10.0.0.11", "RemotePort": 50001},
        {"LocalPort": 60000, "RemoteAddress": "10.0.0.12", "RemotePort": 5432},
    ],
}


# --------------------------------------------------------------------------- SSH server


@dataclass
class FakeHost:
    """Answers the collector's fixed commands like a Linux host would."""

    linux_output: str = LINUX_OUTPUT
    docker_info: dict[str, Any] | None = field(default_factory=lambda: dict(DOCKER_INFO))
    containers: list[dict[str, Any]] = field(default_factory=lambda: list(CONTAINERS))
    username: str = "ops"
    password: str = "s3cret"
    commands: list[str] = field(default_factory=list)

    def reply(self, command: str) -> tuple[int, str, str]:
        self.commands.append(command)
        if command == "sh -s":
            return 0, self.linux_output, ""
        if command.startswith("uname"):
            return 0, "Linux 6.8.0 x86_64\napp-01\n", ""
        if command.startswith("docker info"):
            if self.docker_info is None:
                return 1, "", "permission denied while trying to connect to the Docker daemon"
            return 0, json.dumps(self.docker_info), ""
        if command.startswith("docker ps"):
            return 0, json.dumps(self.containers), ""
        return 127, "", f"unexpected command: {command}"


async def start_ssh_server(host: FakeHost, key: asyncssh.SSHKey) -> asyncssh.SSHAcceptor:
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            return username == host.username and password == host.password

    async def handle(process: asyncssh.SSHServerProcess[str]) -> None:
        command = process.command or ""
        if command == "sh -s":
            await process.stdin.read()
        status, out, err = host.reply(command)
        process.stdout.write(out)
        process.stderr.write(err)
        process.exit(status)

    return await asyncssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[key], process_factory=handle
    )


def port_of(server: asyncssh.SSHAcceptor) -> int:
    return int(server.sockets[0].getsockname()[1])
