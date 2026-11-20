"""Qdrant snapshot backup and restore, one snapshot file per collection.

Per-collection snapshots (rather than a full-storage snapshot) can be restored through
the HTTP API on a running server, so restore needs no Qdrant restart or CLI flags.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import httpx

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_TIMEOUT = httpx.Timeout(30.0, read=600.0)


def _client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url.rstrip("/"), timeout=_TIMEOUT, trust_env=False)


def collections(url: str) -> list[str]:
    with _client(url) as client:
        r = client.get("/collections")
        r.raise_for_status()
        return sorted(c["name"] for c in r.json()["result"]["collections"])


def snapshot(url: str, out_dir: Path) -> list[Path]:
    """Snapshot every collection into ``out_dir/<collection>.snapshot``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with _client(url) as client:
        for name in collections(url):
            if not _SAFE_NAME.match(name):
                raise ValueError(f"unexpected collection name {name!r}")
            col = quote(name, safe="")
            r = client.post(f"/collections/{col}/snapshots", params={"wait": "true"})
            r.raise_for_status()
            snap = quote(r.json()["result"]["name"], safe="")
            target = out_dir / f"{name}.snapshot"
            try:
                with client.stream("GET", f"/collections/{col}/snapshots/{snap}") as resp:
                    resp.raise_for_status()
                    with target.open("wb") as fh:
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
            finally:
                # Don't leave a copy of every backup inside Qdrant's own storage.
                client.delete(f"/collections/{col}/snapshots/{snap}")
            written.append(target)
    return written


def restore(url: str, in_dir: Path) -> list[str]:
    """Recover each ``<collection>.snapshot`` in ``in_dir`` (replacing existing data)."""
    restored: list[str] = []
    with _client(url) as client:
        for path in sorted(in_dir.glob("*.snapshot")):
            name = path.stem
            if not _SAFE_NAME.match(name):
                raise ValueError(f"unexpected snapshot file {path.name!r}")
            with path.open("rb") as fh:
                r = client.post(
                    f"/collections/{quote(name, safe='')}/snapshots/upload",
                    params={"priority": "snapshot", "wait": "true"},
                    files={"snapshot": (path.name, fh, "application/octet-stream")},
                )
            r.raise_for_status()
            restored.append(name)
    return restored
