"""Knowledge base search tool (local; contacts no host)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.knowledge.safety import wrap_untrusted
from app.knowledge.search import hybrid_search
from app.tools.base import StrictArgs, ToolOutput, ToolSpec
from app.tools.context import ToolContext

SearchableType = Literal["runbook", "troubleshooting", "documentation", "procedure", "any"]
EXCERPT_CHARS = 1200


class KnowledgeArgs(StrictArgs):
    query: str = Field(
        description="What to look for: symptoms, error messages, service names",
        min_length=3,
        max_length=300,
    )
    document_type: SearchableType = Field(
        "any", description="Restrict to one kind of document (default: any)"
    )
    resource_id: str | None = Field(
        None, description="Optional resource: documents linked to it rank higher"
    )
    limit: int = Field(5, ge=1, le=10)


async def search_knowledge(ctx: ToolContext, args: KnowledgeArgs) -> ToolOutput:
    types: list[str] = (
        ["runbook", "troubleshooting", "documentation", "procedure"]
        if args.document_type == "any"
        else [str(args.document_type)]
    )
    boost: list[uuid.UUID] = [ctx.resource.id] if ctx.resource else []
    async with ctx.db() as session:
        result = await hybrid_search(
            session,
            ctx.settings,
            ctx.tenant_id,
            args.query,
            document_types=types,
            boost_resources=boost,
            limit=args.limit,
        )
    if not result.hits:
        note = f" ({result.note})" if result.note else ""
        return ToolOutput("(no matching documents)", f"No knowledge documents matched{note}")
    blocks = []
    for n, hit in enumerate(result.hits, start=1):
        flags = ", validated" if hit.validated else ""
        where = f" — {hit.heading}" if hit.heading else ""
        header = f"[{n}] {hit.title} ({hit.document_type}, v{hit.version}{flags}){where}"
        body = hit.content if len(hit.content) <= EXCERPT_CHARS else hit.excerpt
        blocks.append(f"{header}\n{wrap_untrusted(body, source=f'{hit.title} v{hit.version}')}")
    return ToolOutput(
        "\n\n".join(blocks),
        f"{len(result.hits)} knowledge document section(s) matched ({result.mode} search)",
        {"hits": [h.as_dict() for h in result.hits], "mode": result.mode},
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_knowledge",
        "Search the knowledge base (runbooks, troubleshooting guides, documentation) for "
        "procedures related to the symptoms. Results are reference material, not proof.",
        KnowledgeArgs,
        search_knowledge,
        remote=False,
        category="knowledge",
    ),
)
