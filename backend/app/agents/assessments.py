"""Deterministic specialists: Knowledge, Incident Triage and Change Impact.

These answer from LocalOps' own records (inventory, dependency graph, incidents and
past investigations) with explainable rules rather than a model, following the
spec's hybrid approach: deterministic logic where an answer must be reproducible.
Each returns evidence items (with ids) and findings that cite them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.diagnostics import COVERAGE
from app.agents.toolloop import AgentFindings, Hypothesis, Observation
from app.core.config import Settings
from app.knowledge.search import hybrid_search
from app.models import OPEN_STATUSES, Incident, IncidentResource, Resource, incident_key
from app.services import knowledge_search
from app.services import resource_service as rs

SEVERITY_PRIORITY = {
    "critical": "P1",
    "high": "P2",
    "medium": "P3",
    "low": "P4",
    "informational": "P4",
}
_SEVERITY_ORDER = ("informational", "low", "medium", "high", "critical")
PRODUCTION = ("production", "prod", "prd", "live")


@dataclass(slots=True)
class Assessment:
    evidence: list[dict[str, Any]] = field(default_factory=list)
    findings: AgentFindings = field(default_factory=AgentFindings)
    data: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def as_finding(self, task_id: str, agent: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "agent": agent,
            "status": "completed" if self.evidence else "skipped",
            "reason": None if self.evidence else self.note,
            "evidence": self.evidence,
            "summary": self.findings.summary,
            "observations": [o.model_dump() for o in self.findings.observations],
            "hypotheses": [h.model_dump() for h in self.findings.hypotheses],
            "notes": [self.note] if self.note and self.evidence else [],
            "tool_calls": 0,
            "data": self.data,
        }


def _is_production(env: str | None) -> bool:
    return (env or "").strip().lower() in PRODUCTION


def _ids(resources: list[dict[str, Any]]) -> list[uuid.UUID]:
    return [uuid.UUID(r["resource_id"]) for r in resources if r.get("resolved")]


async def _open_incidents(
    session: AsyncSession, resource_ids: list[uuid.UUID], exclude: uuid.UUID | None = None
) -> dict[uuid.UUID, list[str]]:
    if not resource_ids:
        return {}
    stmt = (
        select(IncidentResource.resource_id, Incident.number)
        .join(Incident, Incident.id == IncidentResource.incident_id)
        .where(IncidentResource.resource_id.in_(resource_ids), Incident.status.in_(OPEN_STATUSES))
    )
    if exclude is not None:
        stmt = stmt.where(Incident.id != exclude)
    out: dict[uuid.UUID, list[str]] = {}
    for rid, number in (await session.execute(stmt)).all():
        out.setdefault(rid, []).append(incident_key(number))
    return out


# ---------------------------------------------------------------------- knowledge

RUNBOOK_TYPES = ("runbook", "troubleshooting", "procedure", "documentation")


async def knowledge(
    session: AsyncSession,
    text: str,
    resources: list[dict[str, Any]],
    prefix: str,
    *,
    exclude_investigation: uuid.UUID | None = None,
    exclude_incident: uuid.UUID | None = None,
    settings: Settings | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Assessment:
    ids = _ids(resources)
    incidents = await knowledge_search.similar_incidents(
        session, text, ids, exclude_id=exclude_incident, limit=4
    )
    investigations = await knowledge_search.similar_investigations(
        session, text, ids, exclude_id=exclude_investigation, limit=3
    )
    result = Assessment()
    observations: list[Observation] = []
    for item in [*incidents, *investigations]:
        eid = f"{prefix}.{len(result.evidence) + 1}"
        label = (
            f"Incident {item['number']} “{item['title']}” ({item['status']})"
            if item["kind"] == "incident"
            else f"Investigation “{item['title']}”"
        )
        cause = item.get("root_cause")
        summary = label + (f": root cause {cause}" if cause else ": no root cause recorded")
        lines = [summary]
        if item.get("resolution"):
            lines.append(f"resolution: {item['resolution']}")
        if item.get("conclusion") and not cause:
            lines.append(f"conclusion: {item['conclusion']}")
        result.evidence.append(
            {
                "id": eid,
                "source": "knowledge",
                "agent": "knowledge",
                "tool": "search_similar_incidents",
                "resource": None,
                "summary": summary[:500],
                "excerpt": "\n".join(lines)[:1500],
                "ok": True,
                "ref": {"kind": item["kind"], "id": item["id"], "number": item.get("number")},
                "score": item["score"],
                "observed_at": item["created_at"],
            }
        )
        observations.append(Observation(statement=summary[:500], evidence_ids=[eid]))

    # Runbooks, troubleshooting guides and documentation for these symptoms.
    runbooks: list[dict[str, Any]] = []
    search_note: str | None = None
    if settings is not None and tenant_id is not None:
        found = await hybrid_search(
            session,
            settings,
            tenant_id,
            text,
            document_types=list(RUNBOOK_TYPES),
            boost_resources=ids,
            limit=3,
            per_document=1,
        )
        search_note = found.note
        for hit in found.hits:
            eid = f"{prefix}.{len(result.evidence) + 1}"
            where = f" — {hit.heading}" if hit.heading else ""
            summary = f"{hit.document_type.title()} “{hit.title}” v{hit.version}{where}" + (
                " (validated)" if hit.validated else ""
            )
            runbooks.append({**hit.as_dict(), "evidence_id": eid})
            result.evidence.append(
                {
                    "id": eid,
                    "source": "knowledge",
                    "agent": "knowledge",
                    "tool": "search_knowledge",
                    "resource": None,
                    "summary": summary[:500],
                    "excerpt": hit.content[:1500],
                    "ok": True,
                    "ref": {
                        "kind": "document",
                        "id": str(hit.document_id),
                        "chunk_id": str(hit.chunk_id),
                        "version": hit.version,
                    },
                    "score": round(hit.score, 4),
                    "match": hit.match,
                    "observed_at": None,
                }
            )
            observations.append(
                Observation(statement=f"Relevant procedure: {summary}"[:500], evidence_ids=[eid])
            )
    if result.evidence:
        parts = []
        if incidents or investigations:
            parts.append(
                f"{len(incidents)} similar incident(s) and {len(investigations)} similar "
                "investigation(s) found"
            )
        if runbooks:
            parts.append(f"{len(runbooks)} relevant knowledge document(s)")
        solutions = [i["resolution"] for i in incidents if i.get("resolution")][:3]
        reuse = f" Earlier resolutions: {'; '.join(solutions)}." if solutions else ""
        result.findings = AgentFindings(
            summary=(
                f"{' and '.join(parts)}.{reuse} Past causes and runbooks are context, not "
                "proof for this case."
            )[:1500],
            observations=observations,
        )
        result.note = search_note
    elif search_note:
        result.note = f"No similar incidents or documents found ({search_note})"
    else:
        result.note = "No similar past incidents, investigations or documents were found"
    result.data = {
        "similar_incidents": incidents,
        "similar_investigations": investigations,
        "runbooks": runbooks,
    }
    return result


# ------------------------------------------------------------------------- triage


async def triage(
    session: AsyncSession,
    resources: list[dict[str, Any]],
    prefix: str,
    *,
    urgency: str | None = None,
    incident_id: uuid.UUID | None = None,
) -> Assessment:
    result = Assessment(note="No affected resource was resolved, so impact cannot be assessed")
    ids = _ids(resources)
    open_by_resource = await _open_incidents(session, ids, exclude=incident_id)
    worst = "low"
    reasons: list[str] = []
    paths: list[str] = []
    for rid in ids:
        r = await session.get(Resource, rid)
        if r is None:
            continue
        dependents = await rs.dependents_by_depth(session, rid)
        prod = _is_production(r.environment)
        others = open_by_resource.get(rid, [])
        level = {"unhealthy": "medium", "degraded": "low"}.get(r.health_status, "low")
        if r.health_status == "unhealthy" and prod:
            level = "critical" if len(dependents) >= 3 else "high"
        elif r.health_status in ("unhealthy", "degraded") and (prod or len(dependents) >= 3):
            level = "high" if r.health_status == "unhealthy" else "medium"
        if _SEVERITY_ORDER.index(level) > _SEVERITY_ORDER.index(worst):
            worst = level
        eid = f"{prefix}.{len(result.evidence) + 1}"
        summary = (
            f"{r.name}: health {r.health_status}, environment {r.environment or 'unset'}, "
            f"{len(dependents)} dependent resource(s)"
            + (f", other open incidents {', '.join(others)}" if others else "")
        )
        result.evidence.append(
            {
                "id": eid,
                "source": "inventory",
                "agent": "incident_triage",
                "tool": "triage",
                "resource_id": str(r.id),
                "resource": r.name,
                "resource_type": r.resource_type,
                "summary": summary,
                "excerpt": "; ".join(r.health_reasons or []) or "no health reasons recorded",
                "ok": True,
                "observed_at": (r.last_health_at or r.created_at).isoformat(),
            }
        )
        reasons.append(f"{summary} ({eid})")
        agent = COVERAGE.get(r.resource_type)
        if agent:
            paths.append(f"{agent.replace('_', ' ')} for {r.name}")
    if not result.evidence:
        return result
    if urgency in ("high", "critical") and _SEVERITY_ORDER.index(worst) < 2:
        worst = "medium"  # the operator's stated urgency keeps it from being ranked trivial
    priority = SEVERITY_PRIORITY[worst]
    all_ids = [e["id"] for e in result.evidence]
    result.findings = AgentFindings(
        summary=f"Suggested severity {worst}, priority {priority}.",
        observations=[
            Observation(
                statement=f"Suggested severity {worst} (priority {priority})", evidence_ids=all_ids
            ),
            *(Observation(statement=e["summary"], evidence_ids=[e["id"]]) for e in result.evidence),
        ],
    )
    result.data = {
        "severity": worst,
        "priority": priority,
        "reasons": reasons,
        "investigation_paths": [
            *list(dict.fromkeys(paths)),
            "knowledge search for similar incidents",
        ],
    }
    return result


# ------------------------------------------------------------------ change impact


async def change_impact(
    session: AsyncSession, resources: list[dict[str, Any]], prefix: str
) -> Assessment:
    result = Assessment(note="No target resource was resolved in the inventory")
    targets = _ids(resources)
    for rid in targets:
        r = await session.get(Resource, rid)
        if r is None:
            continue
        depths = await rs.dependents_by_depth(session, rid)
        dep_rows = (
            list(await session.scalars(select(Resource).where(Resource.id.in_(list(depths)))))
            if depths
            else []
        )
        direct = sorted(x.name for x in dep_rows if depths[x.id] == 1)
        indirect = sorted(x.name for x in dep_rows if depths[x.id] > 1)
        prod_dependents = sorted(x.name for x in dep_rows if _is_production(x.environment))
        unwell = sorted(x.name for x in dep_rows if x.health_status in ("unhealthy", "degraded"))
        open_map = await _open_incidents(session, [rid, *depths])
        active = sorted({n for ns in open_map.values() for n in ns})
        previous = int(
            await session.scalar(
                select(func.count())
                .select_from(IncidentResource)
                .where(IncidentResource.resource_id == rid)
            )
            or 0
        )
        prod = _is_production(r.environment)
        score = (
            (2 if prod else 0)
            + (1 if direct else 0)
            + (1 if len(depths) >= 5 else 0)
            + (1 if prod_dependents and not prod else 0)
            + (1 if active else 0)
            + (1 if unwell else 0)
        )
        risk = "high" if score >= 4 else "medium" if score >= 2 else "low"
        if score >= 6:
            risk = "critical"
        precautions = []
        if direct or indirect:
            precautions.append(
                f"Notify the owners of the {len(depths)} dependent resource(s) and schedule a "
                "maintenance window."
            )
        if prod:
            precautions.append(
                "Production resource: prepare and test a rollback before the change."
            )
        if active:
            precautions.append(
                f"Resolve or account for active incidents first: {', '.join(active)}."
            )
        if unwell:
            precautions.append(
                f"Dependents already unhealthy or degraded: {', '.join(unwell[:8])}."
            )
        if r.resource_type in ("linux_server", "windows_server", "docker_host"):
            precautions.append("Confirm workloads on the host can be drained or restarted safely.")
        precautions.append("Verify health of the resource and its dependents after the change.")
        approvals = (
            [
                "Production change: approval by an operator "
                "(enforced by the approval policy in phase 5)"
            ]
            if prod or prod_dependents
            else ["Standard change: owner acknowledgement"]
        )
        eid = f"{prefix}.{len(result.evidence) + 1}"
        summary = (
            f"{r.name} ({r.environment or 'no environment'}): {len(direct)} direct and "
            f"{len(indirect)} indirect dependent(s); risk {risk}"
        )
        impact = {
            "resource_id": str(r.id),
            "resource": r.name,
            "environment": r.environment,
            "owner": r.owner,
            "direct": direct,
            "indirect": indirect,
            "production_dependents": prod_dependents,
            "unhealthy_dependents": unwell,
            "active_incidents": active,
            "previous_incidents": previous,
            "risk_level": risk,
            "potential_disruption": (
                f"{', '.join((direct + indirect)[:10])} may be disrupted"
                if direct or indirect
                else "No dependent resources are recorded"
            ),
            "precautions": precautions,
            "required_approvals": approvals,
            "maintenance_windows": "not tracked yet",
        }
        result.evidence.append(
            {
                "id": eid,
                "source": "inventory",
                "agent": "change_impact",
                "tool": "dependency_graph",
                "resource_id": str(r.id),
                "resource": r.name,
                "resource_type": r.resource_type,
                "summary": summary,
                "excerpt": (
                    f"direct: {', '.join(direct) or 'none'}\n"
                    f"indirect: {', '.join(indirect) or 'none'}\n"
                    f"owner: {r.owner or 'unset'}; previous incidents: {previous}; "
                    f"active incidents: {', '.join(active) or 'none'}"
                ),
                "ok": True,
                "observed_at": (r.last_seen_at or r.created_at).isoformat(),
            }
        )
        result.data.setdefault("impacts", []).append(impact)
    if result.evidence:
        impacts = result.data["impacts"]
        overall = max(
            (i["risk_level"] for i in impacts), key=["low", "medium", "high", "critical"].index
        )
        result.data["risk_level"] = overall
        result.findings = AgentFindings(
            summary=f"Overall change risk: {overall}.",
            observations=[
                Observation(statement=e["summary"], kind="confirmed_fact", evidence_ids=[e["id"]])
                for e in result.evidence
            ],
            hypotheses=[
                Hypothesis(
                    statement=f"Changing {i['resource']} may disrupt: {i['potential_disruption']}",
                    likelihood="high" if i["direct"] else "low",
                    evidence_ids=[e["id"]],
                )
                for i, e in zip(impacts, result.evidence, strict=True)
                if i["direct"] or i["indirect"]
            ],
        )
    return result
