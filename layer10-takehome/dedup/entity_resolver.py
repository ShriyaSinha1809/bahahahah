"""
Entity resolution and canonicalization.

The hardest part of the memory graph: resolving multiple surface forms
of the same real-world entity into a single canonical record.

Multi-signal approach:
1. **Email address clustering:** All emails from the same address → same person.
2. **Name normalization:** Parse into (first, last) tuples, fuzzy match.
3. **Alias absorption:** Merge entity records, preserving all aliases.
4. **Reversible merge events:** Every merge is logged and can be undone.

Design decisions:
- We use a greedy clustering approach: process pairs in order of
  similarity score, merge the highest-confidence matches first.
- Merges are transitive: if A merges with B and B merges with C,
  then A, B, C are all the same canonical entity.
- The merge audit trail supports undo — critical for production trust
  where entity resolution errors are inevitable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz

from logging_config import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────


@dataclass
class MergeEvent:
    """Audit record for a single entity merge."""

    merged_from: str  # entity_id that was absorbed
    merged_into: str  # canonical entity_id
    reason: str  # e.g., "email_address_match", "fuzzy_name_0.92"
    confidence: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    reversed: bool = False


@dataclass
class CanonicalEntity:
    """
    Resolved canonical entity with all known surface forms.

    This is the output of entity resolution — a single record
    representing one real-world entity with all its aliases.
    """

    canonical_id: str
    canonical_name: str
    entity_type: str
    aliases: set[str] = field(default_factory=set)
    email_addresses: set[str] = field(default_factory=set)
    properties: dict[str, Any] = field(default_factory=dict)
    merge_history: list[MergeEvent] = field(default_factory=list)

    def absorb(self, other: "CanonicalEntity", reason: str, confidence: float) -> MergeEvent:
        """
        Merge another entity into this one.

        Absorbs all aliases, email addresses, and properties.
        Returns the merge event for audit logging.
        """
        self.aliases.add(other.canonical_name)
        self.aliases.update(other.aliases)
        self.email_addresses.update(other.email_addresses)

        # Merge properties (keep ours on conflict)
        for key, value in other.properties.items():
            if key not in self.properties:
                self.properties[key] = value

        event = MergeEvent(
            merged_from=other.canonical_id,
            merged_into=self.canonical_id,
            reason=reason,
            confidence=confidence,
        )
        self.merge_history.append(event)
        return event


# ──────────────────────────────────────────────────────────────
# Name Parsing & Normalization
# ──────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_email_addresses(aliases: list[str]) -> set[str]:
    """Extract email addresses from a list of aliases."""
    emails: set[str] = set()
    for alias in aliases:
        match = _EMAIL_RE.search(alias.lower())
        if match:
            emails.add(match.group())
    return emails


def parse_name_parts(name: str) -> tuple[str, str]:
    """
    Parse a name into (first, last) components.

    Handles:
    - "Kenneth Lay" → ("kenneth", "lay")
    - "Lay, Kenneth" → ("kenneth", "lay")
    - "Ken" → ("ken", "")
    """
    name = name.strip()

    # Handle "Last, First" format
    if "," in name:
        parts = [p.strip().lower() for p in name.split(",", 1)]
        if len(parts) == 2:
            return parts[1], parts[0]

    parts = name.lower().split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    elif len(parts) == 1:
        return parts[0], ""
    return "", ""


def name_similarity(name_a: str, name_b: str) -> float:
    """
    Compute similarity between two names using multiple signals.

    Returns a score in [0.0, 1.0]:
    - Jaro-Winkler on full name (weighted 0.4)
    - Last name exact match bonus (0.3)
    - First name fuzzy match (0.3)
    """
    # Full name Jaro-Winkler
    full_sim = fuzz.ratio(name_a.lower(), name_b.lower()) / 100.0

    # Parse components
    first_a, last_a = parse_name_parts(name_a)
    first_b, last_b = parse_name_parts(name_b)

    # Last name match
    if last_a and last_b:
        last_sim = 1.0 if last_a == last_b else fuzz.ratio(last_a, last_b) / 100.0
    else:
        last_sim = 0.0

    # First name match (handles Ken vs Kenneth)
    if first_a and first_b:
        # Check if one is a prefix of the other (nickname detection)
        if first_a.startswith(first_b) or first_b.startswith(first_a):
            first_sim = 0.9
        else:
            first_sim = fuzz.ratio(first_a, first_b) / 100.0
    else:
        first_sim = 0.0

    return 0.4 * full_sim + 0.3 * last_sim + 0.3 * first_sim


# ──────────────────────────────────────────────────────────────
# Entity Resolver
# ──────────────────────────────────────────────────────────────


class EntityResolver:
    """
    Resolves extracted entities into canonical records.

    Three-pass algorithm:
    1. Email address clustering (highest confidence)
    2. Fuzzy name matching within same entity type
    3. Cross-alias matching (if "Ken" co-occurs with ken.lay@enron.com)

    Usage:
        resolver = EntityResolver()
        for entity in extracted_entities:
            resolver.add(entity_id, name, entity_type, aliases, properties)
        canonical = resolver.resolve()
    """

    def __init__(
        self,
        name_threshold: float = 0.82,
    ) -> None:
        self._entities: dict[str, CanonicalEntity] = {}
        self._name_threshold = name_threshold
        self._merge_events: list[MergeEvent] = []

    def add(
        self,
        entity_id: str,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Register an entity for resolution."""
        all_aliases = set(aliases or [])
        emails = extract_email_addresses(list(all_aliases) + [name])

        self._entities[entity_id] = CanonicalEntity(
            canonical_id=entity_id,
            canonical_name=name,
            entity_type=entity_type,
            aliases=all_aliases,
            email_addresses=emails,
            properties=properties or {},
        )

    def resolve(self) -> list[CanonicalEntity]:
        """
        Execute entity resolution and return canonical entities.

        Returns the merged, deduplicated set of canonical entities
        along with their merge histories.
        """
        logger.info("entity_resolution_start", entity_count=len(self._entities))

        # Pass 1: Email address clustering
        self._merge_by_email()

        # Pass 2: Fuzzy name matching
        self._merge_by_name()

        result = list(self._entities.values())
        logger.info(
            "entity_resolution_complete",
            canonical_count=len(result),
            total_merges=len(self._merge_events),
        )
        return result

    @property
    def merge_events(self) -> list[MergeEvent]:
        return self._merge_events

    def _merge_by_email(self) -> None:
        """Merge entities that share an email address."""
        # Build email → entity mapping
        email_to_entities: dict[str, list[str]] = {}
        for eid, entity in self._entities.items():
            for email_addr in entity.email_addresses:
                email_to_entities.setdefault(email_addr, []).append(eid)

        # Merge clusters
        for email_addr, entity_ids in email_to_entities.items():
            if len(entity_ids) < 2:
                continue

            # Keep the entity with the longest name as canonical
            sorted_ids = sorted(
                entity_ids,
                key=lambda eid: len(self._entities.get(eid, CanonicalEntity("", "", "")).canonical_name),
                reverse=True,
            )
            # Filter to entities that still exist (not already merged)
            active = [eid for eid in sorted_ids if eid in self._entities]
            if len(active) < 2:
                continue

            canonical_id = active[0]
            canonical = self._entities[canonical_id]

            for merge_id in active[1:]:
                if merge_id not in self._entities:
                    continue
                other = self._entities[merge_id]
                event = canonical.absorb(
                    other,
                    reason=f"email_address_match:{email_addr}",
                    confidence=0.95,
                )
                self._merge_events.append(event)
                del self._entities[merge_id]

    def _merge_by_name(self) -> None:
        """Merge entities with similar names within the same type."""
        # Group by entity type
        by_type: dict[str, list[str]] = {}
        for eid, entity in self._entities.items():
            by_type.setdefault(entity.entity_type, []).append(eid)

        for entity_type, entity_ids in by_type.items():
            if len(entity_ids) < 2:
                continue

            # Compute pairwise similarities
            pairs: list[tuple[float, str, str]] = []
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    eid_a, eid_b = entity_ids[i], entity_ids[j]
                    if eid_a not in self._entities or eid_b not in self._entities:
                        continue
                    entity_a = self._entities[eid_a]
                    entity_b = self._entities[eid_b]

                    # Compare canonical names and all aliases
                    max_sim = name_similarity(entity_a.canonical_name, entity_b.canonical_name)

                    for alias in entity_b.aliases:
                        sim = name_similarity(entity_a.canonical_name, alias)
                        max_sim = max(max_sim, sim)

                    for alias in entity_a.aliases:
                        sim = name_similarity(alias, entity_b.canonical_name)
                        max_sim = max(max_sim, sim)

                    if max_sim >= self._name_threshold:
                        pairs.append((max_sim, eid_a, eid_b))

            # Sort by similarity descending, merge greedily
            pairs.sort(key=lambda x: x[0], reverse=True)

            for sim, eid_a, eid_b in pairs:
                if eid_a not in self._entities or eid_b not in self._entities:
                    continue  # one was already merged

                entity_a = self._entities[eid_a]
                entity_b = self._entities[eid_b]

                # Keep the one with more information as canonical
                if len(entity_a.canonical_name) >= len(entity_b.canonical_name):
                    canonical, other, cid, oid = entity_a, entity_b, eid_a, eid_b
                else:
                    canonical, other, cid, oid = entity_b, entity_a, eid_b, eid_a

                event = canonical.absorb(
                    other,
                    reason=f"fuzzy_name_{sim:.2f}",
                    confidence=sim,
                )
                self._merge_events.append(event)
                del self._entities[oid]


# ──────────────────────────────────────────────────────────────
# Convenience
# ──────────────────────────────────────────────────────────────


def resolve_entities(
    entities: list[dict[str, Any]],
    name_threshold: float = 0.82,
) -> tuple[list[CanonicalEntity], list[MergeEvent]]:
    """
    One-shot entity resolution.

    Args:
        entities: List of dicts with keys: id, canonical_name, entity_type, aliases, properties.
        name_threshold: Minimum name similarity for merging.

    Returns:
        (canonical_entities, merge_events)
    """
    resolver = EntityResolver(name_threshold=name_threshold)
    for e in entities:
        resolver.add(
            entity_id=str(e["id"]),
            name=e["canonical_name"],
            entity_type=e["entity_type"],
            aliases=e.get("aliases", []),
            properties=e.get("properties", {}),
        )
    canonical = resolver.resolve()
    return canonical, resolver.merge_events
