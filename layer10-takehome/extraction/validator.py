"""
Extraction result validator.

Validates LLM extraction output against:
1. JSON syntax (with retry feedback)
2. Pydantic schema conformance
3. Evidence grounding — verifies each excerpt exists in the source email
4. Confidence thresholds
5. Entity name normalization

Design decisions:
- Validation is strict: claims without verifiable evidence are dropped,
  not kept with a warning. This is the core Layer10 principle.
- Fuzzy matching (Levenshtein) is used as a repair mechanism for small
  LLM transcription errors in excerpts, not as a way to accept hallucinations.
- All dropped/repaired claims are logged for audit and quality monitoring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from config import get_settings
from extraction.schema import (
    ExtractionResult,
    ExtractedClaim,
    ExtractedEntity,
)
from logging_config import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Validation Result Model
# ──────────────────────────────────────────────────────────────


@dataclass
class ValidationEvent:
    """Record of a validation action (drop, repair, or pass)."""

    action: str  # "dropped", "repaired", "passed"
    target: str  # "claim" or "entity"
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Output of the validation pipeline."""

    extraction: ExtractionResult
    events: list[ValidationEvent] = field(default_factory=list)
    is_valid: bool = True

    @property
    def dropped_count(self) -> int:
        return sum(1 for e in self.events if e.action == "dropped")

    @property
    def repaired_count(self) -> int:
        return sum(1 for e in self.events if e.action == "repaired")


# ──────────────────────────────────────────────────────────────
# JSON Parsing
# ──────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _extract_json(raw: str) -> str:
    """
    Extract JSON from LLM response, handling markdown fences.

    LLMs sometimes wrap JSON in ```json ... ``` blocks despite instructions.
    """
    # Try to find fenced JSON
    match = _JSON_FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()

    # Try to find JSON object directly
    stripped = raw.strip()
    if stripped.startswith("{"):
        return stripped

    # Last resort: find first { to last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]

    return raw


def parse_llm_json(raw_response: str) -> dict[str, Any] | None:
    """
    Parse JSON from LLM response with error handling.

    Returns None if JSON is completely unrecoverable.
    """
    json_str = _extract_json(raw_response)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        logger.warning("json_parse_failed", error=str(exc), preview=json_str[:200])
        return None


# ──────────────────────────────────────────────────────────────
# Evidence Verification
# ──────────────────────────────────────────────────────────────


def _find_excerpt_in_body(excerpt: str, body: str) -> tuple[bool, int | None, int | None]:
    """
    Check if an evidence excerpt exists in the email body.

    Returns (found, start_offset, end_offset).
    First tries exact match, then fuzzy partial match.
    """
    # Exact substring match
    idx = body.find(excerpt)
    if idx != -1:
        return True, idx, idx + len(excerpt)

    # Normalized match (collapse whitespace)
    norm_body = " ".join(body.split())
    norm_excerpt = " ".join(excerpt.split())
    idx = norm_body.find(norm_excerpt)
    if idx != -1:
        return True, None, None  # offsets don't map cleanly after normalization

    # Fuzzy partial match — only accept if very close (>90% similarity)
    ratio = fuzz.partial_ratio(norm_excerpt, norm_body)
    if ratio >= 90:
        return True, None, None

    return False, None, None


# ──────────────────────────────────────────────────────────────
# Entity Name Normalization
# ──────────────────────────────────────────────────────────────


def _normalize_person_name(name: str) -> str:
    """
    Normalize a person's name to Title Case.

    Handles: "LAY, KENNETH" → "Kenneth Lay"
             "ken lay" → "Ken Lay"
    """
    # Handle "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2:
            name = f"{parts[1]} {parts[0]}"

    return " ".join(name.title().split())


def _normalize_entity_name(name: str, entity_type: str) -> str:
    """Normalize entity name based on type."""
    if entity_type == "Person":
        return _normalize_person_name(name)
    return " ".join(name.strip().split())


# ──────────────────────────────────────────────────────────────
# Validator
# ──────────────────────────────────────────────────────────────


class ExtractionValidator:
    """
    Validates and repairs LLM extraction output.

    Usage:
        validator = ExtractionValidator()
        result = validator.validate(raw_json_str, email_body)
    """

    def __init__(self, min_confidence: float | None = None) -> None:
        settings = get_settings()
        self._min_confidence = min_confidence or settings.extraction_min_confidence

    def validate(
        self,
        raw_response: str,
        email_body: str,
        email_id: str = "",
    ) -> ValidationResult:
        """
        Full validation pipeline.

        Steps:
        1. Parse JSON
        2. Validate against Pydantic schema
        3. Verify evidence excerpts
        4. Normalize entity names
        5. Filter by confidence threshold
        """
        events: list[ValidationEvent] = []

        # Step 1: Parse JSON
        parsed = parse_llm_json(raw_response)
        if parsed is None:
            events.append(
                ValidationEvent(
                    action="dropped",
                    target="extraction",
                    reason="json_parse_failure",
                    details={"email_id": email_id},
                )
            )
            return ValidationResult(
                extraction=ExtractionResult(),
                events=events,
                is_valid=False,
            )

        # Step 2: Pydantic validation
        try:
            extraction = ExtractionResult.model_validate(parsed)
        except Exception as exc:
            events.append(
                ValidationEvent(
                    action="dropped",
                    target="extraction",
                    reason="schema_validation_failure",
                    details={"error": str(exc), "email_id": email_id},
                )
            )
            # Try partial recovery — extract what we can
            extraction = self._partial_recovery(parsed, events)
            if extraction is None:
                return ValidationResult(
                    extraction=ExtractionResult(),
                    events=events,
                    is_valid=False,
                )

        # Step 3: Normalize entity names
        normalized_entities: list[ExtractedEntity] = []
        for entity in extraction.entities:
            norm_name = _normalize_entity_name(entity.name, entity.type.value)
            if norm_name != entity.name:
                events.append(
                    ValidationEvent(
                        action="repaired",
                        target="entity",
                        reason="name_normalized",
                        details={"original": entity.name, "normalized": norm_name},
                    )
                )
            normalized_entities.append(
                entity.model_copy(update={"name": norm_name})
            )
        extraction.entities = normalized_entities

        # Step 4: Verify evidence & filter claims
        valid_claims: list[ExtractedClaim] = []
        for claim in extraction.claims:
            # Confidence check
            if claim.confidence < self._min_confidence:
                events.append(
                    ValidationEvent(
                        action="dropped",
                        target="claim",
                        reason="below_confidence_threshold",
                        details={
                            "claim_type": claim.type.value,
                            "confidence": claim.confidence,
                            "threshold": self._min_confidence,
                        },
                    )
                )
                continue

            # Evidence verification
            found, start, end = _find_excerpt_in_body(
                claim.evidence_excerpt, email_body
            )
            if not found:
                events.append(
                    ValidationEvent(
                        action="dropped",
                        target="claim",
                        reason="evidence_not_found",
                        details={
                            "claim_type": claim.type.value,
                            "excerpt_preview": claim.evidence_excerpt[:100],
                        },
                    )
                )
                continue

            valid_claims.append(claim)
            events.append(
                ValidationEvent(
                    action="passed",
                    target="claim",
                    reason="validated",
                    details={
                        "claim_type": claim.type.value,
                        "has_offsets": start is not None,
                    },
                )
            )

        extraction.claims = valid_claims

        result = ValidationResult(
            extraction=extraction,
            events=events,
            is_valid=len(extraction.entities) > 0,
        )

        logger.info(
            "validation_complete",
            email_id=email_id,
            entities=len(extraction.entities),
            claims=len(extraction.claims),
            dropped=result.dropped_count,
            repaired=result.repaired_count,
        )

        return result

    def _partial_recovery(
        self,
        parsed: dict[str, Any],
        events: list[ValidationEvent],
    ) -> ExtractionResult | None:
        """
        Try to recover valid entities and claims from partially invalid JSON.

        This handles cases where the LLM output has some valid and some
        invalid items — we keep what we can rather than dropping everything.
        """
        entities: list[ExtractedEntity] = []
        claims: list[ExtractedClaim] = []

        # Recover entities
        for raw_entity in parsed.get("entities", []):
            try:
                entities.append(ExtractedEntity.model_validate(raw_entity))
            except Exception:
                events.append(
                    ValidationEvent(
                        action="dropped",
                        target="entity",
                        reason="partial_recovery_failed",
                        details={"raw": str(raw_entity)[:200]},
                    )
                )

        # Recover claims
        for raw_claim in parsed.get("claims", []):
            try:
                claims.append(ExtractedClaim.model_validate(raw_claim))
            except Exception:
                events.append(
                    ValidationEvent(
                        action="dropped",
                        target="claim",
                        reason="partial_recovery_failed",
                        details={"raw": str(raw_claim)[:200]},
                    )
                )

        if not entities and not claims:
            return None

        return ExtractionResult(entities=entities, claims=claims)


# ──────────────────────────────────────────────────────────────
# Convenience
# ──────────────────────────────────────────────────────────────


def validate_extraction(
    raw_response: str,
    email_body: str,
    email_id: str = "",
    min_confidence: float | None = None,
) -> ValidationResult:
    """One-shot validation of an LLM extraction response."""
    return ExtractionValidator(min_confidence=min_confidence).validate(
        raw_response, email_body, email_id
    )
