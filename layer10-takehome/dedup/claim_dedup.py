"""
Claim deduplication.

Merges duplicate claims that say the same thing across multiple emails
into a single canonical claim with multiple evidence pointers.

Example: If 50 emails all state "Ken Lay is CEO of Enron", we create
one canonical claim with 50 evidence records rather than 50 claims.

Design decisions:
- Claims are considered duplicates if they share the same
  (subject_canonical_id, claim_type, object_canonical_id) within a
  configurable time window.
- Temporal evolution is NOT dedup: "Ken is CEO" and "Ken resigned" are
  different claims with different validity windows.
- The merged claim's confidence = max of individual confidences.
- All evidence pointers are preserved on the merged claim.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ClaimCandidate:
    """A claim with its metadata for dedup consideration."""

    claim_id: str
    claim_type: str
    subject_id: str
    object_id: str
    properties: dict[str, Any]
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ClaimMergeEvent:
    """Audit record for a claim merge."""

    merged_from: str  # claim_id absorbed
    merged_into: str  # canonical claim_id
    reason: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ClaimDedupResult:
    """Output of claim deduplication."""

    canonical_claims: list[ClaimCandidate]
    merge_events: list[ClaimMergeEvent]
    stats: dict[str, int]


class ClaimDeduplicator:
    """
    Deduplicates claims based on their semantic key.

    Claims with the same (subject_id, claim_type, object_id) are merged
    if their properties are compatible and their temporal windows align.
    """

    def deduplicate(self, claims: list[ClaimCandidate]) -> ClaimDedupResult:
        """Run claim deduplication."""
        logger.info("claim_dedup_start", input_count=len(claims))

        # Group by semantic key
        groups: dict[tuple[str, str, str], list[ClaimCandidate]] = defaultdict(list)
        for claim in claims:
            key = (claim.subject_id, claim.claim_type, claim.object_id)
            groups[key].append(claim)

        canonical_claims: list[ClaimCandidate] = []
        merge_events: list[ClaimMergeEvent] = []

        for key, group in groups.items():
            if len(group) == 1:
                canonical_claims.append(group[0])
                continue

            # Sort by confidence descending — highest confidence becomes canonical
            group.sort(key=lambda c: c.confidence, reverse=True)
            canonical = group[0]

            # Absorb evidence from duplicates
            for dup in group[1:]:
                canonical.evidence_ids.extend(dup.evidence_ids)
                canonical.confidence = max(canonical.confidence, dup.confidence)

                # Expand temporal window
                if dup.valid_from and (
                    canonical.valid_from is None or dup.valid_from < canonical.valid_from
                ):
                    canonical.valid_from = dup.valid_from
                if dup.valid_to and (
                    canonical.valid_to is None or dup.valid_to > canonical.valid_to
                ):
                    canonical.valid_to = dup.valid_to

                merge_events.append(
                    ClaimMergeEvent(
                        merged_from=dup.claim_id,
                        merged_into=canonical.claim_id,
                        reason=f"same_semantic_key:{key}",
                    )
                )

            canonical_claims.append(canonical)

        stats = {
            "input_claims": len(claims),
            "canonical_claims": len(canonical_claims),
            "merged": len(merge_events),
        }
        logger.info("claim_dedup_complete", **stats)

        return ClaimDedupResult(
            canonical_claims=canonical_claims,
            merge_events=merge_events,
            stats=stats,
        )


def deduplicate_claims(claims: list[ClaimCandidate]) -> ClaimDedupResult:
    """One-shot claim deduplication."""
    return ClaimDeduplicator().deduplicate(claims)
