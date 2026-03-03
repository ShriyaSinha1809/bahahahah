"""
Pydantic models for the extraction pipeline.

These models define the contract between the LLM extractor, the validator,
and the storage layer. Every field is typed and documented.

Design decisions:
- Pydantic v2 for performance and strict validation.
- Entity and Claim models mirror the DB schema but are independent —
  the storage layer maps these to SQL, not vice versa.
- Evidence is tightly coupled to claims: every claim MUST have at least
  one evidence pointer. This is enforced by ExtractionResult's validator.
- Confidence scores use a constrained float [0.0, 1.0].
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────


class EntityType(str, Enum):
    PERSON = "Person"
    ORGANIZATION = "Organization"
    PROJECT = "Project"
    TOPIC = "Topic"
    DOCUMENT = "Document"
    MEETING = "Meeting"


class ClaimType(str, Enum):
    WORKS_AT = "WORKS_AT"
    REPORTS_TO = "REPORTS_TO"
    PARTICIPATES_IN = "PARTICIPATES_IN"
    DISCUSSES = "DISCUSSES"
    DECIDED = "DECIDED"
    MENTIONS = "MENTIONS"
    SENT_TO = "SENT_TO"
    REFERENCES_DOC = "REFERENCES_DOC"
    SCHEDULED = "SCHEDULED"


# ──────────────────────────────────────────────────────────────
# Entity Model
# ──────────────────────────────────────────────────────────────


class ExtractedEntity(BaseModel):
    """
    An entity extracted from an email by the LLM.

    Entities are the nodes of the memory graph: people, organizations,
    projects, topics, documents, and meetings.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Primary name of the entity as mentioned in the email.",
    )
    type: EntityType = Field(
        ...,
        description="Category of the entity.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names, abbreviations, or email addresses.",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional structured properties (role, title, department, etc.).",
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        """Strip whitespace and normalize."""
        return " ".join(v.split()).strip()

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, v: list[str]) -> list[str]:
        """Strip and deduplicate aliases."""
        seen: set[str] = set()
        result: list[str] = []
        for alias in v:
            cleaned = alias.strip()
            lower = cleaned.lower()
            if cleaned and lower not in seen:
                seen.add(lower)
                result.append(cleaned)
        return result


# ──────────────────────────────────────────────────────────────
# Claim Model
# ──────────────────────────────────────────────────────────────


class ExtractedClaim(BaseModel):
    """
    A relationship/claim extracted from an email.

    Claims are the edges of the memory graph: they connect a subject entity
    to an object entity with a typed relationship, grounded by an evidence
    excerpt from the source email.
    """

    type: ClaimType = Field(
        ...,
        description="The relationship type.",
    )
    subject: str = Field(
        ...,
        min_length=1,
        description="Name of the subject entity (must match an extracted entity).",
    )
    object: str = Field(
        ...,
        min_length=1,
        description="Name of the object entity (must match an extracted entity).",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional properties (role, status, etc.).",
    )
    evidence_excerpt: str = Field(
        ...,
        min_length=1,
        description="Verbatim substring from the email that supports this claim.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score: 0.9+ explicit, 0.6-0.8 inferred.",
    )

    @field_validator("subject", "object")
    @classmethod
    def clean_entity_ref(cls, v: str) -> str:
        return " ".join(v.split()).strip()


# ──────────────────────────────────────────────────────────────
# Extraction Result (top-level container)
# ──────────────────────────────────────────────────────────────


class ExtractionResult(BaseModel):
    """
    Complete extraction output from a single email.

    Contains all entities and claims extracted by the LLM, after validation.
    """

    entities: list[ExtractedEntity] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claim_references(self) -> "ExtractionResult":
        """
        Ensure every claim's subject and object reference a known entity.

        This catches hallucinated entity references early in the pipeline.
        """
        entity_names = {e.name.lower() for e in self.entities}
        entity_aliases = set()
        for e in self.entities:
            for alias in e.aliases:
                entity_aliases.add(alias.lower())

        all_known = entity_names | entity_aliases

        valid_claims: list[ExtractedClaim] = []
        for claim in self.claims:
            if claim.subject.lower() in all_known and claim.object.lower() in all_known:
                valid_claims.append(claim)
            # Silently drop claims with unresolvable entity references
            # (these are logged by the validator module)

        self.claims = valid_claims
        return self


# ──────────────────────────────────────────────────────────────
# Evidence Model (for storage layer)
# ──────────────────────────────────────────────────────────────


class EvidenceRecord(BaseModel):
    """
    Evidence pointer linking a claim to its source material.

    Stored in the evidence table. Includes character offsets for
    precise source highlighting in the UI.
    """

    source_type: str = "email"
    source_id: str = Field(..., description="message_id of the source email")
    excerpt: str = Field(..., description="Verbatim text from the source")
    char_offset_start: int | None = None
    char_offset_end: int | None = None
    source_timestamp: datetime | None = None
    extraction_version: str = Field(..., description="Version tag of the extraction run")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ──────────────────────────────────────────────────────────────
# Extraction Version Tag
# ──────────────────────────────────────────────────────────────


class ExtractionVersionInfo(BaseModel):
    """Metadata about an extraction run for reproducibility."""

    version_tag: str
    model_name: str
    prompt_hash: str
    schema_version: str = "v1.0"
    notes: str = ""
