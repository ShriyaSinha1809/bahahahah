"""
Tests for the extraction layer — schema, prompts, and validator.
"""

from __future__ import annotations

import json

import pytest

from extraction.schema import (
    EntityType,
    ClaimType,
    ExtractedEntity,
    ExtractedClaim,
    ExtractionResult,
)
from extraction.validator import (
    ExtractionValidator,
    parse_llm_json,
    validate_extraction,
)
from extraction.prompts import get_prompt_hash, get_version_tag, build_user_prompt


# ──────────────────────────────────────────────────────────────
# Schema Tests
# ──────────────────────────────────────────────────────────────


class TestExtractionSchema:
    def test_entity_creation(self) -> None:
        entity = ExtractedEntity(
            name="Kenneth Lay",
            type=EntityType.PERSON,
            aliases=["Ken Lay", "ken.lay@enron.com"],
            properties={"role": "CEO"},
        )
        assert entity.name == "Kenneth Lay"
        assert entity.type == EntityType.PERSON
        assert len(entity.aliases) == 2

    def test_entity_name_normalization(self) -> None:
        entity = ExtractedEntity(
            name="  Kenneth   Lay  ",
            type=EntityType.PERSON,
        )
        assert entity.name == "Kenneth Lay"

    def test_entity_alias_dedup(self) -> None:
        entity = ExtractedEntity(
            name="Test",
            type=EntityType.PERSON,
            aliases=["Ken", "ken", "Ken"],  # duplicates
        )
        assert len(entity.aliases) == 1

    def test_claim_creation(self) -> None:
        claim = ExtractedClaim(
            type=ClaimType.WORKS_AT,
            subject="Kenneth Lay",
            object="Enron Corp",
            evidence_excerpt="Ken Lay is the CEO of Enron",
            confidence=0.95,
        )
        assert claim.confidence == 0.95

    def test_claim_confidence_bounds(self) -> None:
        with pytest.raises(Exception):
            ExtractedClaim(
                type=ClaimType.WORKS_AT,
                subject="A",
                object="B",
                evidence_excerpt="text",
                confidence=1.5,  # out of bounds
            )

    def test_extraction_result_validates_references(self) -> None:
        result = ExtractionResult(
            entities=[
                ExtractedEntity(name="Alice", type=EntityType.PERSON),
                ExtractedEntity(name="Acme Corp", type=EntityType.ORGANIZATION),
            ],
            claims=[
                ExtractedClaim(
                    type=ClaimType.WORKS_AT,
                    subject="Alice",
                    object="Acme Corp",
                    evidence_excerpt="Alice works at Acme",
                    confidence=0.9,
                ),
                ExtractedClaim(
                    type=ClaimType.WORKS_AT,
                    subject="Unknown Person",
                    object="Acme Corp",
                    evidence_excerpt="text",
                    confidence=0.9,
                ),
            ],
        )
        # Unknown Person claim should be dropped
        assert len(result.claims) == 1
        assert result.claims[0].subject == "Alice"


# ──────────────────────────────────────────────────────────────
# Validator Tests
# ──────────────────────────────────────────────────────────────


SAMPLE_EMAIL_BODY = (
    "I am pleased to announce that Jeff Skilling will be joining "
    "our executive team as President and COO."
)


class TestValidator:
    def test_parse_valid_json(self) -> None:
        raw = json.dumps({"entities": [], "claims": []})
        result = parse_llm_json(raw)
        assert result is not None
        assert result["entities"] == []

    def test_parse_json_with_markdown_fence(self) -> None:
        raw = '```json\n{"entities": [], "claims": []}\n```'
        result = parse_llm_json(raw)
        assert result is not None

    def test_parse_invalid_json(self) -> None:
        result = parse_llm_json("not json at all")
        assert result is None

    def test_validate_with_valid_evidence(self) -> None:
        raw = json.dumps({
            "entities": [
                {"name": "Jeff Skilling", "type": "Person"},
                {"name": "Executive Team", "type": "Organization"},
            ],
            "claims": [
                {
                    "type": "WORKS_AT",
                    "subject": "Jeff Skilling",
                    "object": "Executive Team",
                    "evidence_excerpt": "Jeff Skilling will be joining our executive team",
                    "confidence": 0.9,
                },
            ],
        })
        result = validate_extraction(raw, SAMPLE_EMAIL_BODY, "test001")
        assert result.is_valid
        assert len(result.extraction.claims) == 1

    def test_validate_drops_hallucinated_evidence(self) -> None:
        raw = json.dumps({
            "entities": [
                {"name": "Jeff Skilling", "type": "Person"},
                {"name": "Board", "type": "Organization"},
            ],
            "claims": [
                {
                    "type": "REPORTS_TO",
                    "subject": "Jeff Skilling",
                    "object": "Board",
                    "evidence_excerpt": "This text does not appear in the email at all",
                    "confidence": 0.9,
                },
            ],
        })
        result = validate_extraction(raw, SAMPLE_EMAIL_BODY, "test001")
        assert len(result.extraction.claims) == 0

    def test_validate_drops_low_confidence(self) -> None:
        raw = json.dumps({
            "entities": [
                {"name": "Jeff Skilling", "type": "Person"},
                {"name": "Executive Team", "type": "Organization"},
            ],
            "claims": [
                {
                    "type": "WORKS_AT",
                    "subject": "Jeff Skilling",
                    "object": "Executive Team",
                    "evidence_excerpt": "Jeff Skilling will be joining our executive team",
                    "confidence": 0.1,  # below threshold
                },
            ],
        })
        result = validate_extraction(raw, SAMPLE_EMAIL_BODY, "test001")
        assert len(result.extraction.claims) == 0


# ──────────────────────────────────────────────────────────────
# Prompt Tests
# ──────────────────────────────────────────────────────────────


class TestPrompts:
    def test_prompt_hash_deterministic(self) -> None:
        h1 = get_prompt_hash()
        h2 = get_prompt_hash()
        assert h1 == h2
        assert len(h1) == 12

    def test_version_tag_format(self) -> None:
        tag = get_version_tag("test-model")
        assert tag.startswith("v1.0_test-model_")

    def test_build_user_prompt(self) -> None:
        prompt = build_user_prompt(
            sender="ken@enron.com",
            recipients=["jeff@enron.com"],
            date="2002-01-14",
            subject="Test",
            body="Hello world",
        )
        assert "ken@enron.com" in prompt
        assert "Hello world" in prompt
        assert "METADATA:" in prompt
