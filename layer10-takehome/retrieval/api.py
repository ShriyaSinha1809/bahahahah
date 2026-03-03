"""
FastAPI retrieval API.

Serves the memory graph through evidence-backed endpoints.
Every response traces back to source emails — no ungrounded claims.

Endpoints:
  GET /api/query?q=...              → ContextPack (full graph answer)
  GET /api/entity/{id}              → Entity details + aliases
  GET /api/entity/{id}/claims       → Claims for an entity
  GET /api/claim/{id}/evidence      → Evidence for a claim
  GET /api/graph                    → Graph visualization data
  GET /api/stats                    → Pipeline statistics

Design decisions:
- All endpoints return Pydantic models (auto-serialized by FastAPI).
- Database sessions are scoped per-request via dependency injection.
- Errors return structured JSON, not HTML.
- CORS enabled for frontend development.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text

from config import get_settings
from logging_config import get_logger, setup_logging
from retrieval.context_pack import (
    ContextPack,
    EntitySummary,
    ClaimWithEvidence,
    EvidenceSnippet,
    assemble_context_pack,
)
from retrieval.linker import link_entities
from retrieval.traversal import expand_entity_graph
from storage.db import (
    ClaimRepository,
    EntityRepository,
    EvidenceRepository,
    get_session,
    init_db,
    close_db,
)

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# App Lifecycle
# ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and teardown resources."""
    setup_logging()
    await init_db()
    logger.info("api_started")
    yield
    await close_db()
    logger.info("api_stopped")


app = FastAPI(
    title="Layer10 Memory Graph API",
    description="Evidence-backed organizational memory graph from the Enron email corpus.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────
# Response Models
# ──────────────────────────────────────────────────────────────


class EntityDetail(BaseModel):
    """Full entity details with claims summary."""

    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    claim_count: int = 0


class GraphData(BaseModel):
    """Graph visualization data."""

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class StatsResponse(BaseModel):
    """Pipeline statistics."""

    total_emails: int = 0
    total_entities: int = 0
    total_claims: int = 0
    total_evidence: int = 0


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────


@app.get("/api/query", response_model=ContextPack)
async def query(
    q: str = Query(..., min_length=1, description="Natural language question"),
    include_historical: bool = Query(False, description="Include non-current claims"),
    depth: int = Query(1, ge=1, le=3, description="Graph expansion depth"),
    min_confidence: float = Query(0.5, ge=0.0, le=1.0),
) -> ContextPack:
    """
    Answer a question using the memory graph.

    Pipeline: question → entity linking → graph expansion → context pack.
    Every claim in the response is backed by evidence from source emails.
    """
    async with get_session() as session:
        # Step 1: Link question to entities
        candidates = await link_entities(session, q)

        if not candidates:
            return ContextPack(question=q)

        entity_ids = [str(c["id"]) for c in candidates]

        # Step 2: Expand graph
        graph_data = await expand_entity_graph(
            session,
            entity_ids,
            depth=depth,
            include_historical=include_historical,
            min_confidence=min_confidence,
        )

        # Step 3: Assemble context pack
        return assemble_context_pack(q, graph_data)


@app.get("/api/entity/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: str) -> EntityDetail:
    """Get full details for a specific entity."""
    async with get_session() as session:
        entity = await EntityRepository.get_by_id(session, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        claims = await ClaimRepository.get_for_entity(session, entity_id)

        return EntityDetail(
            entity_id=str(entity["id"]),
            canonical_name=entity["canonical_name"],
            entity_type=entity["entity_type"],
            aliases=entity.get("aliases", []),
            properties=entity.get("properties", {}),
            claim_count=len(claims),
        )


@app.get("/api/entity/{entity_id}/claims", response_model=list[ClaimWithEvidence])
async def get_entity_claims(
    entity_id: str,
    claim_type: str | None = Query(None, description="Filter by claim type"),
    current_only: bool = Query(True),
) -> list[ClaimWithEvidence]:
    """Get all claims involving a specific entity, with evidence."""
    async with get_session() as session:
        entity = await EntityRepository.get_by_id(session, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        claims = await ClaimRepository.get_for_entity(
            session, entity_id, claim_type=claim_type, current_only=current_only
        )

        result: list[ClaimWithEvidence] = []
        for claim in claims:
            cid = str(claim["id"])
            evidence_records = await EvidenceRepository.get_for_claim(session, cid)

            snippets = [
                EvidenceSnippet(
                    source_id=ev.get("source_id", ""),
                    excerpt=ev.get("excerpt", ""),
                    source_date=ev.get("source_timestamp"),
                    sender=ev.get("sender", ""),
                    subject=ev.get("email_subject", ""),
                    extraction_version=ev.get("extraction_version", ""),
                )
                for ev in evidence_records
            ]

            result.append(
                ClaimWithEvidence(
                    claim_id=cid,
                    claim_type=claim["claim_type"],
                    subject=claim.get("subject_name", ""),
                    object=claim.get("object_name", ""),
                    properties=claim.get("properties", {}),
                    confidence=claim["confidence"],
                    valid_from=claim.get("valid_from"),
                    valid_to=claim.get("valid_to"),
                    is_current=claim.get("is_current", True),
                    evidence=snippets,
                )
            )

        return result


@app.get("/api/claim/{claim_id}/evidence", response_model=list[EvidenceSnippet])
async def get_claim_evidence(claim_id: str) -> list[EvidenceSnippet]:
    """Get all evidence supporting a specific claim."""
    async with get_session() as session:
        claim = await ClaimRepository.get_by_id(session, claim_id)
        if not claim:
            raise HTTPException(status_code=404, detail="Claim not found")

        evidence_records = await EvidenceRepository.get_for_claim(session, claim_id)

        return [
            EvidenceSnippet(
                source_id=ev.get("source_id", ""),
                excerpt=ev.get("excerpt", ""),
                source_date=ev.get("source_timestamp"),
                sender=ev.get("sender", ""),
                subject=ev.get("email_subject", ""),
                extraction_version=ev.get("extraction_version", ""),
            )
            for ev in evidence_records
        ]


@app.get("/api/graph", response_model=GraphData)
async def get_graph(
    center_entity: str | None = Query(None, description="Center entity ID"),
    depth: int = Query(2, ge=1, le=3),
    min_confidence: float = Query(0.5, ge=0.0, le=1.0),
) -> GraphData:
    """
    Get graph data for visualization.

    If center_entity is provided, returns the subgraph around it.
    Otherwise returns a summary of the most-connected entities.
    """
    async with get_session() as session:
        if center_entity:
            entity = await EntityRepository.get_by_id(session, center_entity)
            if not entity:
                raise HTTPException(status_code=404, detail="Entity not found")

            graph_data = await expand_entity_graph(
                session,
                [center_entity],
                depth=depth,
                min_confidence=min_confidence,
            )
        else:
            # Return top entities by claim count
            result = await session.execute(
                text("""
                    SELECT e.*, COUNT(c.id) as claim_count
                    FROM entities e
                    LEFT JOIN claims c ON e.id = c.subject_id OR e.id = c.object_id
                    GROUP BY e.id
                    ORDER BY claim_count DESC
                    LIMIT 20
                """)
            )
            top_entities = [dict(row._mapping) for row in result.fetchall()]
            entity_ids = [str(e["id"]) for e in top_entities[:10]]

            if not entity_ids:
                return GraphData()

            graph_data = await expand_entity_graph(
                session, entity_ids, depth=1, min_confidence=min_confidence
            )

        # Simplify for visualization
        nodes = [
            {
                "id": str(n["id"]),
                "label": n["canonical_name"],
                "type": n["entity_type"],
                "aliases": n.get("aliases", []),
            }
            for n in graph_data["nodes"]
        ]
        edges = [
            {
                "id": str(e["id"]),
                "source": str(e["subject_id"]),
                "target": str(e["object_id"]),
                "type": e["claim_type"],
                "confidence": e["confidence"],
                "label": e["claim_type"],
            }
            for e in graph_data["edges"]
        ]

        return GraphData(nodes=nodes, edges=edges)


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get pipeline statistics."""
    async with get_session() as session:
        emails = await session.execute(text("SELECT COUNT(*) FROM raw_emails"))
        entities = await session.execute(text("SELECT COUNT(*) FROM entities"))
        claims = await session.execute(text("SELECT COUNT(*) FROM claims"))
        evidence = await session.execute(text("SELECT COUNT(*) FROM evidence"))

        return StatsResponse(
            total_emails=emails.scalar() or 0,
            total_entities=entities.scalar() or 0,
            total_claims=claims.scalar() or 0,
            total_evidence=evidence.scalar() or 0,
        )


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
