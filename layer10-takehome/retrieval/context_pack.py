"""
Context pack assembly.

Assembles the final structured context pack that answers a query.
This is what gets returned by the /api/query endpoint — a complete,
evidence-backed response suitable for consumption by an LLM or UI.

Design decisions:
- The context pack includes both the graph data AND conflicting claims,
  so the consumer can see both sides of any disagreement.
- Evidence snippets include source metadata (sender, date, subject)
  for attribution.
- Total evidence count helps the consumer gauge confidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────
# Response Models
# ──────────────────────────────────────────────────────────────


class EvidenceSnippet(BaseModel):
    """A single evidence excerpt with source metadata."""

    source_id: str
    excerpt: str
    source_date: datetime | None = None
    sender: str = ""
    subject: str = ""
    extraction_version: str = ""


class ClaimWithEvidence(BaseModel):
    """A claim with all its supporting evidence."""

    claim_id: str
    claim_type: str
    subject: str
    object: str
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_current: bool = True
    evidence: list[EvidenceSnippet] = Field(default_factory=list)


class EntitySummary(BaseModel):
    """Summary of an entity in the context pack."""

    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class ConflictPair(BaseModel):
    """Two claims that conflict with each other."""

    claim_a: ClaimWithEvidence
    claim_b: ClaimWithEvidence
    conflict_reason: str


class ContextPack(BaseModel):
    """
    Complete response to a query — entities, claims, evidence, conflicts.

    This is the primary output of the retrieval pipeline.
    """

    question: str
    entities: list[EntitySummary] = Field(default_factory=list)
    claims: list[ClaimWithEvidence] = Field(default_factory=list)
    conflicts: list[ConflictPair] = Field(default_factory=list)
    total_evidence_count: int = 0
    applied_user_filter: str | None = None  # set when user_id permission filter is active


# ──────────────────────────────────────────────────────────────
# Assembly
# ──────────────────────────────────────────────────────────────


def assemble_context_pack(
    question: str,
    graph_data: dict[str, Any],
) -> ContextPack:
    """
    Assemble a ContextPack from graph traversal results.

    Args:
        question: The original query.
        graph_data: Output from expand_entity_graph() with nodes, edges, evidence_map.
    """
    # Build entity summaries
    entities: list[EntitySummary] = []
    for node in graph_data.get("nodes", []):
        entities.append(
            EntitySummary(
                entity_id=str(node["id"]),
                canonical_name=node["canonical_name"],
                entity_type=node["entity_type"],
                aliases=node.get("aliases", []),
                properties=node.get("properties", {}),
            )
        )

    # Build claims with evidence
    evidence_map: dict[str, list[dict[str, Any]]] = graph_data.get("evidence_map", {})
    claims: list[ClaimWithEvidence] = []
    total_evidence = 0

    for edge in graph_data.get("edges", []):
        cid = str(edge["id"])
        evidence_records = evidence_map.get(cid, [])
        total_evidence += len(evidence_records)

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

        claims.append(
            ClaimWithEvidence(
                claim_id=cid,
                claim_type=edge["claim_type"],
                subject=edge.get("subject_name", ""),
                object=edge.get("object_name", ""),
                properties=edge.get("properties", {}),
                confidence=edge["confidence"],
                valid_from=edge.get("valid_from"),
                valid_to=edge.get("valid_to"),
                is_current=edge.get("is_current", True),
                evidence=snippets,
            )
        )

    # Detect conflicts
    conflicts = _detect_conflicts(claims)

    return ContextPack(
        question=question,
        entities=entities,
        claims=claims,
        conflicts=conflicts,
        total_evidence_count=total_evidence,
    )


def _detect_conflicts(claims: list[ClaimWithEvidence]) -> list[ConflictPair]:
    """
    Detect conflicting claims.

    Two claims conflict if they have the same subject, claim type,
    and different objects with overlapping validity windows.
    Example: Person X WORKS_AT Org A and Person X WORKS_AT Org B
    with overlapping time ranges.
    """
    conflicts: list[ConflictPair] = []

    # Group by (subject, claim_type)
    groups: dict[tuple[str, str], list[ClaimWithEvidence]] = {}
    for claim in claims:
        key = (claim.subject, claim.claim_type)
        groups.setdefault(key, []).append(claim)

    for key, group in groups.items():
        if len(group) < 2:
            continue

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.object != b.object:
                    # Check temporal overlap
                    if _temporal_overlap(a, b):
                        conflicts.append(
                            ConflictPair(
                                claim_a=a,
                                claim_b=b,
                                conflict_reason=(
                                    f"Same subject '{a.subject}' has conflicting "
                                    f"'{a.claim_type}' claims with overlapping time ranges"
                                ),
                            )
                        )

    return conflicts


def _temporal_overlap(a: ClaimWithEvidence, b: ClaimWithEvidence) -> bool:
    """Check if two claims have overlapping validity windows."""
    # If either has no temporal bounds, treat as potentially overlapping
    if a.valid_from is None and a.valid_to is None:
        return True
    if b.valid_from is None and b.valid_to is None:
        return True

    a_start = a.valid_from or datetime.min
    a_end = a.valid_to or datetime.max
    b_start = b.valid_from or datetime.min
    b_end = b.valid_to or datetime.max

    return a_start <= b_end and b_start <= a_end
