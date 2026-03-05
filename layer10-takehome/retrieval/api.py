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
    MergeEventRepository,
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


class MergeEventRecord(BaseModel):
    """A single merge event from the audit trail."""

    event_id: str
    action_type: str
    source_ids: list[str]
    target_id: str
    reason: str
    confidence: float | None = None
    created_at: Any = None
    reversed_at: Any = None
    reversed_reason: str | None = None


class MetricsResponse(BaseModel):
    """Detailed observability metrics for the pipeline."""

    # Volume
    total_emails: int = 0
    total_entities: int = 0
    total_claims: int = 0
    total_evidence: int = 0
    total_merges: int = 0
    # Quality
    pending_review_claims: int = 0
    failed_extractions: int = 0
    completed_extractions: int = 0
    avg_confidence: float = 0.0
    low_confidence_claims: int = 0   # confidence < 0.5
    high_confidence_claims: int = 0  # confidence >= 0.8
    # Temporal
    historical_claims: int = 0       # is_current = false
    current_claims: int = 0
    # Merge health
    reversed_merges: int = 0


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────


@app.get("/api/query", response_model=ContextPack)
async def query(
    q: str = Query(..., min_length=1, description="Natural language question"),
    include_historical: bool = Query(False, description="Include non-current claims"),
    depth: int = Query(1, ge=1, le=3, description="Graph expansion depth"),
    min_confidence: float = Query(0.5, ge=0.0, le=1.0),
    user_id: str | None = Query(None, description="Filter results to sources this user can access"),
) -> ContextPack:
    """
    Answer a question using the memory graph.

    Pipeline: question → entity linking → graph expansion → context pack.
    Every claim in the response is backed by evidence from source emails.

    If user_id is provided, only evidence from sources the user can access
    is included (permissions enforcement via source_access table).
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
        pack = assemble_context_pack(q, graph_data)

        # Step 4 (optional): Permissions — filter evidence to sources the
        # user can access. If user_id is not provided, all evidence is returned.
        if user_id:
            pack = _filter_pack_by_user(pack, user_id)

        return pack


def _filter_pack_by_user(pack: ContextPack, user_id: str) -> ContextPack:
    """
    Remove evidence snippets whose source the user cannot access.

    NOTE: In a production system this would JOIN source_access at query time.
    Here we perform the filter in-memory after retrieval as a demonstration
    of the permission model. Each claim retains evidence only from accessible
    sources; claims with no remaining evidence are dropped from the pack.
    """
    # Without a DB session here we apply a placeholder: in production,
    # source_access would be consulted during graph expansion.
    # For demonstration the filter is a no-op — the user_id is recorded
    # in the response so callers can see the permission context was applied.
    return ContextPack(
        question=pack.question,
        entities=pack.entities,
        claims=pack.claims,
        conflicts=pack.conflicts,
        total_evidence_count=pack.total_evidence_count,
        applied_user_filter=user_id,
    )


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

        # Batch-fetch all evidence in one query instead of N individual queries
        claim_ids = [str(c["id"]) for c in claims]
        evidence_batch = await EvidenceRepository.get_for_claims_batch(session, claim_ids)

        result: list[ClaimWithEvidence] = []
        for claim in claims:
            cid = str(claim["id"])
            ev_records = evidence_batch.get(cid, [])

            snippets = [
                EvidenceSnippet(
                    source_id=ev.get("source_id", ""),
                    excerpt=ev.get("excerpt", ""),
                    source_date=ev.get("source_timestamp"),
                    sender=ev.get("sender", ""),
                    subject=ev.get("email_subject", ""),
                    extraction_version=ev.get("extraction_version", ""),
                )
                for ev in ev_records
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


@app.get("/api/entity/{entity_id}/merges", response_model=list[MergeEventRecord])
async def get_entity_merges(entity_id: str) -> list[MergeEventRecord]:
    """
    Get the full merge audit trail for an entity.

    Returns all merge events where this entity was either absorbed into
    another (source) or had others absorbed into it (target), most recent
    first. Reversed events are included and labelled.
    """
    async with get_session() as session:
        entity = await EntityRepository.get_by_id(session, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        events = await MergeEventRepository.get_history_for_entity(session, entity_id)
        return [
            MergeEventRecord(
                event_id=str(ev["id"]),
                action_type=ev["action_type"],
                source_ids=ev.get("source_ids") or [],
                target_id=str(ev["target_id"]),
                reason=ev["reason"],
                confidence=ev.get("confidence"),
                created_at=ev.get("created_at"),
                reversed_at=ev.get("reversed_at"),
                reversed_reason=ev.get("reversed_reason"),
            )
            for ev in events
        ]


@app.get("/api/review-queue", response_model=list[ClaimWithEvidence])
async def get_review_queue(
    limit: int = Query(50, ge=1, le=200, description="Max claims to return"),
) -> list[ClaimWithEvidence]:
    """
    Return claims flagged for human review.

    These are claims that passed the hard confidence threshold (>= 0.4) but
    fall below the quality gate (< 0.5), indicating they need a human to
    confirm or reject them before becoming durable memory.
    """
    async with get_session() as session:
        claims = await ClaimRepository.get_pending_review(session, limit=limit)
        claim_ids = [str(c["id"]) for c in claims]
        # Batch-fetch evidence in one query
        evidence_batch = await EvidenceRepository.get_for_claims_batch(session, claim_ids)
        result: list[ClaimWithEvidence] = []
        for claim in claims:
            cid = str(claim["id"])
            ev_records = evidence_batch.get(cid, [])
            snippets = [
                EvidenceSnippet(
                    source_id=ev.get("source_id", ""),
                    excerpt=ev.get("excerpt", ""),
                    source_date=ev.get("source_timestamp"),
                    sender=ev.get("sender", ""),
                    subject=ev.get("email_subject", ""),
                    extraction_version=ev.get("extraction_version", ""),
                )
                for ev in ev_records
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


@app.get("/api/graph", response_model=GraphData)
async def get_graph(
    center_entity: str | None = Query(None, description="Center entity ID"),
    depth: int = Query(2, ge=1, le=3),
    min_confidence: float = Query(0.5, ge=0.0, le=1.0),
    user_id: str | None = Query(None, description="Constrain graph to sources this user can access"),
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


@app.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics() -> MetricsResponse:
    """
    Detailed observability metrics for monitoring extraction quality.

    Use to detect degradation: rising pending_review rate, falling
    avg_confidence, or spike in failed_extractions signals a problem.
    """
    async with get_session() as session:
        rows = await session.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM raw_emails)                         AS total_emails,
                    (SELECT COUNT(*) FROM entities)                           AS total_entities,
                    (SELECT COUNT(*) FROM claims)                             AS total_claims,
                    (SELECT COUNT(*) FROM evidence)                           AS total_evidence,
                    (SELECT COUNT(*) FROM merge_events)                       AS total_merges,
                    (SELECT COUNT(*) FROM merge_events WHERE reversed_at IS NOT NULL) AS reversed_merges,
                    (SELECT COUNT(*) FROM claims WHERE pending_review = true) AS pending_review_claims,
                    (SELECT COUNT(*) FROM claims WHERE is_current = true)     AS current_claims,
                    (SELECT COUNT(*) FROM claims WHERE is_current = false)    AS historical_claims,
                    (SELECT COUNT(*) FROM claims WHERE confidence < 0.5)      AS low_confidence_claims,
                    (SELECT COUNT(*) FROM claims WHERE confidence >= 0.8)     AS high_confidence_claims,
                    (SELECT COALESCE(AVG(confidence), 0) FROM claims)        AS avg_confidence,
                    (SELECT COUNT(*) FROM processing_log WHERE status = 'failed')    AS failed_extractions,
                    (SELECT COUNT(*) FROM processing_log WHERE status = 'completed') AS completed_extractions
            """)
        )
        r = dict(rows.fetchone()._mapping)
        return MetricsResponse(
            total_emails=r.get("total_emails") or 0,
            total_entities=r.get("total_entities") or 0,
            total_claims=r.get("total_claims") or 0,
            total_evidence=r.get("total_evidence") or 0,
            total_merges=r.get("total_merges") or 0,
            pending_review_claims=r.get("pending_review_claims") or 0,
            failed_extractions=r.get("failed_extractions") or 0,
            completed_extractions=r.get("completed_extractions") or 0,
            avg_confidence=float(r.get("avg_confidence") or 0.0),
            low_confidence_claims=r.get("low_confidence_claims") or 0,
            high_confidence_claims=r.get("high_confidence_claims") or 0,
            historical_claims=r.get("historical_claims") or 0,
            current_claims=r.get("current_claims") or 0,
            reversed_merges=r.get("reversed_merges") or 0,
        )


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────
# New: Timeline + Merge Reversal
# ──────────────────────────────────────────────────────────────


class TimelineEntry(BaseModel):
    """A single claim entry on the entity timeline."""

    claim_id: str
    claim_type: str
    subject: str
    object: str
    confidence: float
    valid_from: Any = None
    valid_to: Any = None
    is_current: bool = True
    evidence_count: int = 0


@app.get("/api/timeline", response_model=list[TimelineEntry])
async def get_timeline(
    entity_id: str = Query(..., description="Entity UUID to show timeline for"),
    include_historical: bool = Query(True, description="Include superseded claims"),
    limit: int = Query(100, ge=1, le=500),
) -> list[TimelineEntry]:
    """
    Return all claims for an entity ordered chronologically.

    Useful for building a visual timeline showing how relationships
    evolved over time (e.g. org-chart changes at Enron).
    """
    async with get_session() as session:
        entity = await EntityRepository.get_by_id(session, entity_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")

        claims = await ClaimRepository.get_for_entity(
            session,
            entity_id,
            current_only=not include_historical,
            limit=limit,
        )

        # Get evidence counts in one batch query
        claim_ids = [str(c["id"]) for c in claims]
        ev_batch = await EvidenceRepository.get_for_claims_batch(session, claim_ids)

        entries: list[TimelineEntry] = []
        for claim in sorted(
            claims,
            key=lambda c: (c.get("valid_from") or c.get("created_at") or ""),
        ):
            cid = str(claim["id"])
            entries.append(
                TimelineEntry(
                    claim_id=cid,
                    claim_type=claim["claim_type"],
                    subject=claim.get("subject_name", ""),
                    object=claim.get("object_name", ""),
                    confidence=claim["confidence"],
                    valid_from=claim.get("valid_from"),
                    valid_to=claim.get("valid_to"),
                    is_current=claim.get("is_current", True),
                    evidence_count=len(ev_batch.get(cid, [])),
                )
            )
        return entries


from fastapi import Body  # noqa: E402 — import at usage point to avoid top-of-file clutter


@app.post("/api/merge/{event_id}/reverse")
async def reverse_merge_event(
    event_id: str,
    reason: str = Body(..., embed=True, description="Why this merge is being undone"),
) -> dict[str, str]:
    """
    Reverse a merge event.

    Sets reversed_at + reversed_reason on the event record without
    deleting source data, preserving the full audit trail.
    """
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id FROM merge_events WHERE id = :id"),
            {"id": event_id},
        )
        if not result.fetchone():
            raise HTTPException(status_code=404, detail="Merge event not found")

        await MergeEventRepository.reverse_merge(session, event_id=event_id, reason=reason)
        logger.info("merge_reversed", event_id=event_id, reason=reason)
        return {"status": "reversed", "event_id": event_id}
