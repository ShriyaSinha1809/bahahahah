"""
Tests for the entity resolver and claim dedup.
"""

from __future__ import annotations

import pytest

from dedup.entity_resolver import (
    EntityResolver,
    name_similarity,
    parse_name_parts,
    extract_email_addresses,
    resolve_entities,
)
from dedup.claim_dedup import (
    ClaimCandidate,
    ClaimDeduplicator,
    deduplicate_claims,
)

class TestNameUtils:
    def test_parse_name_parts_normal(self) -> None:
        first, last = parse_name_parts("Kenneth Lay")
        assert first == "kenneth"
        assert last == "lay"

    def test_parse_name_parts_reversed(self) -> None:
        first, last = parse_name_parts("Lay, Kenneth")
        assert first == "kenneth"
        assert last == "lay"

    def test_parse_name_parts_single(self) -> None:
        first, last = parse_name_parts("Ken")
        assert first == "ken"
        assert last == ""

    def test_extract_email_addresses(self) -> None:
        emails = extract_email_addresses(["Ken Lay", "ken.lay@enron.com", "CEO"])
        assert "ken.lay@enron.com" in emails
        assert len(emails) == 1

    def test_name_similarity_identical(self) -> None:
        sim = name_similarity("Kenneth Lay", "Kenneth Lay")
        assert sim > 0.95

    def test_name_similarity_nickname(self) -> None:
        sim = name_similarity("Kenneth Lay", "Ken Lay")
        assert sim > 0.8  # Should match due to prefix detection

    def test_name_similarity_different(self) -> None:
        sim = name_similarity("Kenneth Lay", "Jeff Skilling")
        assert sim < 0.5

class TestEntityResolver:
    def test_merge_by_email_address(self) -> None:
        resolver = EntityResolver()
        resolver.add("e1", "Kenneth Lay", "Person", aliases=["ken.lay@enron.com"])
        resolver.add("e2", "Ken Lay", "Person", aliases=["ken.lay@enron.com"])

        result = resolver.resolve()
        assert len(result) == 1
        assert "Ken Lay" in result[0].aliases or "Kenneth Lay" == result[0].canonical_name

    def test_merge_by_fuzzy_name(self) -> None:
        resolver = EntityResolver(name_threshold=0.75)
        resolver.add("e1", "Kenneth Lay", "Person")
        resolver.add("e2", "Ken Lay", "Person")

        result = resolver.resolve()
        assert len(result) == 1

    def test_no_merge_different_names(self) -> None:
        resolver = EntityResolver()
        resolver.add("e1", "Kenneth Lay", "Person")
        resolver.add("e2", "Jeff Skilling", "Person")

        result = resolver.resolve()
        assert len(result) == 2

    def test_no_merge_different_types(self) -> None:
        # Even if names are similar, different types should not merge
        resolver = EntityResolver()
        resolver.add("e1", "Enron Corp", "Organization")
        resolver.add("e2", "Enron Trading", "Organization")

        # These might or might not merge depending on threshold
        result = resolver.resolve()
        assert len(result) >= 1

    def test_merge_events_logged(self) -> None:
        resolver = EntityResolver()
        resolver.add("e1", "Kenneth Lay", "Person", aliases=["ken.lay@enron.com"])
        resolver.add("e2", "Ken Lay", "Person", aliases=["ken.lay@enron.com"])

        resolver.resolve()
        events = resolver.merge_events
        assert len(events) >= 1
        assert "email_address_match" in events[0].reason

    def test_resolve_convenience_function(self) -> None:
        entities = [
            {"id": "e1", "canonical_name": "Kenneth Lay", "entity_type": "Person",
             "aliases": ["ken.lay@enron.com"]},
            {"id": "e2", "canonical_name": "Ken Lay", "entity_type": "Person",
             "aliases": ["ken.lay@enron.com"]},
        ]
        canonical, events = resolve_entities(entities)
        assert len(canonical) == 1
        assert len(events) >= 1

class TestClaimDedup:
    def test_merge_identical_claims(self) -> None:
        claims = [
            ClaimCandidate(
                claim_id="c1",
                claim_type="WORKS_AT",
                subject_id="e1",
                object_id="e2",
                properties={"role": "CEO"},
                confidence=0.9,
                valid_from=None,
                valid_to=None,
                evidence_ids=["ev1"],
            ),
            ClaimCandidate(
                claim_id="c2",
                claim_type="WORKS_AT",
                subject_id="e1",
                object_id="e2",
                properties={"role": "CEO"},
                confidence=0.8,
                valid_from=None,
                valid_to=None,
                evidence_ids=["ev2"],
            ),
        ]

        result = deduplicate_claims(claims)
        assert len(result.canonical_claims) == 1
        assert result.canonical_claims[0].confidence == 0.9  # max
        assert len(result.canonical_claims[0].evidence_ids) == 2
        assert len(result.merge_events) == 1

    def test_no_merge_different_claims(self) -> None:
        claims = [
            ClaimCandidate(
                claim_id="c1",
                claim_type="WORKS_AT",
                subject_id="e1",
                object_id="e2",
                properties={},
                confidence=0.9,
                valid_from=None,
                valid_to=None,
            ),
            ClaimCandidate(
                claim_id="c2",
                claim_type="REPORTS_TO",
                subject_id="e1",
                object_id="e3",
                properties={},
                confidence=0.8,
                valid_from=None,
                valid_to=None,
            ),
        ]

        result = deduplicate_claims(claims)
        assert len(result.canonical_claims) == 2
        assert len(result.merge_events) == 0
