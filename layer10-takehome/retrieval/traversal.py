"""
Graph traversal for context expansion.

Given a set of candidate entities, expands outward through the claim
graph to collect relevant context for answering a question.

Design decisions:
- 1-hop expansion by default (configurable depth).
- Filters by is_current=true unless historical context is requested.
- Caps claims per type to avoid explosion on highly-connected entities.
- Recency bias: recent claims are weighted higher.
- Diversity: ensures multiple claim types are represented.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from storage.db import ClaimRepository, EvidenceRepository
from logging_config import get_logger

logger = get_logger(__name__)


async def expand_entity_graph(
    session: AsyncSession,
    entity_ids: list[str],
    depth: int = 1,
    include_historical: bool = False,
    min_confidence: float = 0.5,
    max_claims_per_type: int = 5,
) -> dict[str, Any]:
    """
    Expand from seed entities to collect graph context.

    Returns a dict with:
    - nodes: entity records (seed + discovered)
    - edges: claim records connecting them
    - evidence_map: claim_id → list of evidence records

    Args:
        entity_ids: Starting entity UUIDs.
        depth: How many hops to expand (1 = direct connections only).
        include_historical: Include claims where is_current=false.
        min_confidence: Minimum claim confidence to include.
        max_claims_per_type: Cap per claim_type to prevent explosion.
    """
    visited_entities: set[str] = set(entity_ids)
    all_claims: list[dict[str, Any]] = []
    evidence_map: dict[str, list[dict[str, Any]]] = {}
    frontier = list(entity_ids)

    for _hop in range(depth):
        next_frontier: list[str] = []

        for eid in frontier:
            claims = await ClaimRepository.get_for_entity(
                session,
                eid,
                current_only=not include_historical,
                limit=50,
            )

            # Filter by confidence
            claims = [c for c in claims if c["confidence"] >= min_confidence]

            # Diversity cap: max N claims per type
            type_counts: dict[str, int] = {}
            filtered_claims: list[dict[str, Any]] = []
            for claim in claims:
                ctype = claim["claim_type"]
                count = type_counts.get(ctype, 0)
                if count < max_claims_per_type:
                    filtered_claims.append(claim)
                    type_counts[ctype] = count + 1

            for claim in filtered_claims:
                all_claims.append(claim)

                # Discover neighbor entities (evidence fetched in batch below)
                for key in ("subject_id", "object_id"):
                    neighbor_id = str(claim[key])
                    if neighbor_id not in visited_entities:
                        visited_entities.add(neighbor_id)
                        next_frontier.append(neighbor_id)

        frontier = next_frontier

    # Batch-fetch evidence for all collected claims in a single query
    all_claim_ids = [str(c["id"]) for c in all_claims]
    if all_claim_ids:
        evidence_map = await EvidenceRepository.get_for_claims_batch(
            session, all_claim_ids
        )

    # Batch-fetch full entity records for all discovered entities in one query
    nodes: list[dict[str, Any]] = []
    if visited_entities:
        result = await session.execute(
            text("SELECT * FROM entities WHERE id = ANY(:ids)"),
            {"ids": list(visited_entities)},
        )
        nodes = [dict(row._mapping) for row in result.fetchall()]

    logger.info(
        "graph_expansion_complete",
        seed_entities=len(entity_ids),
        total_nodes=len(nodes),
        total_edges=len(all_claims),
    )

    return {
        "nodes": nodes,
        "edges": all_claims,
        "evidence_map": evidence_map,
    }
