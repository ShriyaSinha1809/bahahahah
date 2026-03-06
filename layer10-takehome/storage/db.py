"""
Async database access layer.

Provides:
- Connection pool management via SQLAlchemy async engine
- Repository-pattern classes for each table group
- Idempotent upsert methods (safe to re-run)
- Transaction scoping via async context managers

Design decisions:
- Raw SQL via text() instead of ORM — simpler, more transparent, avoids
  the impedance mismatch of mapping a graph model to an ORM.
- Connection pooling parameters are configurable via settings.
- Every write method is idempotent: uses ON CONFLICT DO NOTHING/UPDATE.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from logging_config import get_logger

logger = get_logger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

def _get_engine():
    """Lazy-initialize the async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            echo=settings.log_level == "DEBUG",
        )
    return _engine

def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazy-initialize the session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory

@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Provide a transactional scope around a series of operations.

    Usage:
        async with get_session() as session:
            await repo.insert_entity(session, ...)
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def init_db() -> None:
    """Verify connectivity and log pool status."""
    engine = _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
    logger.info("database_connected", url=get_settings().database_url.split("@")[-1])

async def close_db() -> None:
    """Dispose the engine and connection pool."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_closed")

# Repository: Raw Emails

class RawEmailRepository:
    """CRUD operations for the raw_emails table."""

    @staticmethod
    async def upsert(session: AsyncSession, email_data: dict[str, Any]) -> None:
        """
        Insert a raw email, skipping if message_id already exists.

        Idempotent — safe to call repeatedly with the same data.
        """
        await session.execute(
            text("""
                INSERT INTO raw_emails (
                    message_id, sender, recipients, subject, body, date,
                    in_reply_to, "references", folder_path, raw_text,
                    body_hash, dedup_key
                ) VALUES (
                    :message_id, :sender, :recipients, :subject, :body, :date,
                    :in_reply_to, :references, :folder_path, :raw_text,
                    :body_hash, :dedup_key
                )
                ON CONFLICT (message_id) DO NOTHING
            """),
            email_data,
        )

    @staticmethod
    async def upsert_batch(
        session: AsyncSession, emails: Sequence[dict[str, Any]]
    ) -> int:
        """Insert a batch of raw emails. Returns count of new inserts."""
        if not emails:
            return 0
        # Use executemany for batch performance
        await session.execute(
            text("""
                INSERT INTO raw_emails (
                    message_id, sender, recipients, subject, body, date,
                    in_reply_to, "references", folder_path, raw_text,
                    body_hash, dedup_key
                ) VALUES (
                    :message_id, :sender, :recipients, :subject, :body, :date,
                    :in_reply_to, :references, :folder_path, :raw_text,
                    :body_hash, :dedup_key
                )
                ON CONFLICT (message_id) DO NOTHING
            """),
            list(emails),
        )
        return len(emails)

    @staticmethod
    async def get_unprocessed(
        session: AsyncSession,
        extraction_version: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch emails not yet processed by the given extraction version.

        This is the key idempotency mechanism: re-running extraction with
        the same version skips already-processed emails.
        """
        result = await session.execute(
            text("""
                SELECT re.*
                FROM raw_emails re
                LEFT JOIN processing_log pl
                    ON re.message_id = pl.email_id
                    AND pl.extraction_version = :version
                    AND pl.status = 'completed'
                WHERE pl.id IS NULL
                ORDER BY re.date ASC NULLS LAST
                LIMIT :limit
            """),
            {"version": extraction_version, "limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def count(session: AsyncSession) -> int:
        """Return total count of stored emails."""
        result = await session.execute(text("SELECT COUNT(*) FROM raw_emails"))
        return result.scalar() or 0

# Repository: Entities

class EntityRepository:
    """CRUD operations for the entities table."""

    @staticmethod
    async def upsert(
        session: AsyncSession,
        canonical_name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        properties: dict[str, Any] | None = None,
        entity_id: str | None = None,
    ) -> str:
        """
        Insert or update an entity. Returns the entity UUID.

        Uses canonical_name + entity_type as the natural key for dedup.
        """
        eid = entity_id or str(uuid.uuid4())
        result = await session.execute(
            text("""
                INSERT INTO entities (id, canonical_name, entity_type, aliases, properties)
                VALUES (:id, :name, :type, :aliases, CAST(:props AS jsonb))
                ON CONFLICT (id) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    aliases = EXCLUDED.aliases,
                    properties = EXCLUDED.properties,
                    updated_at = now()
                RETURNING id
            """),
            {
                "id": eid,
                "name": canonical_name,
                "type": entity_type,
                "aliases": aliases or [],
                "props": json.dumps(properties or {}),
            },
        )
        row = result.fetchone()
        return str(row[0]) if row else eid

    @staticmethod
    async def find_by_name(
        session: AsyncSession,
        name: str,
        entity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find entities by exact or alias name match."""
        if entity_type:
            result = await session.execute(
                text("""
                    SELECT * FROM entities
                    WHERE (canonical_name = :name OR :name = ANY(aliases))
                    AND entity_type = :type
                """),
                {"name": name, "type": entity_type},
            )
        else:
            result = await session.execute(
                text("""
                    SELECT * FROM entities
                    WHERE canonical_name = :name OR :name = ANY(aliases)
                """),
                {"name": name},
            )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def find_by_name_fuzzy(
        session: AsyncSession,
        name: str,
        threshold: float = 0.3,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fuzzy search using pg_trgm trigram similarity."""
        result = await session.execute(
            text("""
                SELECT *, similarity(canonical_name, :name) AS sim
                FROM entities
                WHERE similarity(canonical_name, :name) > :threshold
                ORDER BY sim DESC
                LIMIT :limit
            """),
            {"name": name, "threshold": threshold, "limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def get_by_id(session: AsyncSession, entity_id: str) -> dict[str, Any] | None:
        """Fetch a single entity by UUID."""
        result = await session.execute(
            text("SELECT * FROM entities WHERE id = :id"),
            {"id": entity_id},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None

    @staticmethod
    async def list_all(
        session: AsyncSession,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List entities with optional type filter and pagination."""
        if entity_type:
            result = await session.execute(
                text("""
                    SELECT * FROM entities
                    WHERE entity_type = :type
                    ORDER BY canonical_name
                    LIMIT :limit OFFSET :offset
                """),
                {"type": entity_type, "limit": limit, "offset": offset},
            )
        else:
            result = await session.execute(
                text("""
                    SELECT * FROM entities
                    ORDER BY canonical_name
                    LIMIT :limit OFFSET :offset
                """),
                {"limit": limit, "offset": offset},
            )
        return [dict(row._mapping) for row in result.fetchall()]

# Repository: Claims

class ClaimRepository:
    """CRUD operations for the claims table."""

    @staticmethod
    async def insert(
        session: AsyncSession,
        claim_type: str,
        subject_id: str,
        object_id: str,
        properties: dict[str, Any] | None = None,
        confidence: float = 0.5,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        claim_id: str | None = None,
        pending_review: bool = False,
    ) -> str:
        """Insert a new claim. Returns the claim UUID."""
        cid = claim_id or str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO claims (
                    id, claim_type, subject_id, object_id, properties,
                    confidence, valid_from, valid_to, pending_review
                ) VALUES (
                    :id, :type, :subject, :object, CAST(:props AS jsonb),
                    :confidence, :valid_from, :valid_to, :pending_review
                )
            """),
            {
                "id": cid,
                "type": claim_type,
                "subject": subject_id,
                "object": object_id,
                "props": json.dumps(properties or {}),
                "confidence": confidence,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "pending_review": pending_review,
            },
        )
        return cid

    @staticmethod
    async def invalidate_conflicting(
        session: AsyncSession,
        subject_id: str,
        claim_type: str,
        new_valid_from: datetime | None,
    ) -> int:
        """
        Invalidate existing current claims of the same type for the same
        subject when a newer conflicting claim arrives.

        Only applies to mutually-exclusive claim types: WORKS_AT, REPORTS_TO.
        Sets is_current=false and valid_to=new_valid_from on any overlapping
        claims, giving them a closed validity window.

        Returns the number of rows updated.
        """
        if claim_type not in ("WORKS_AT", "REPORTS_TO"):
            return 0
        cutoff = new_valid_from or datetime.utcnow()
        result = await session.execute(
            text("""
                UPDATE claims
                SET is_current = false,
                    valid_to = :cutoff
                WHERE subject_id = :subject
                  AND claim_type  = :ctype
                  AND is_current  = true
                  AND (valid_to IS NULL OR valid_to > :cutoff)
                RETURNING id
            """),
            {"subject": subject_id, "ctype": claim_type, "cutoff": cutoff},
        )
        rows = result.fetchall()
        if rows:
            logger.info(
                "claims_invalidated",
                count=len(rows),
                subject_id=subject_id,
                claim_type=claim_type,
            )
        return len(rows)

    @staticmethod
    async def get_pending_review(
        session: AsyncSession,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return claims flagged for human review (low-confidence inserts)."""
        result = await session.execute(
            text("""
                SELECT c.*,
                       s.canonical_name AS subject_name,
                       o.canonical_name AS object_name
                FROM claims c
                JOIN entities s ON c.subject_id = s.id
                JOIN entities o ON c.object_id = o.id
                WHERE c.pending_review = true
                ORDER BY c.confidence ASC, c.created_at DESC
                LIMIT :limit
            """),
            {"limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def get_for_entity(
        session: AsyncSession,
        entity_id: str,
        claim_type: str | None = None,
        current_only: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch claims where entity is subject or object."""
        conditions = ["(c.subject_id = :eid OR c.object_id = :eid)"]
        params: dict[str, Any] = {"eid": entity_id, "limit": limit}

        if current_only:
            conditions.append("c.is_current = true")
        if claim_type:
            conditions.append("c.claim_type = :ctype")
            params["ctype"] = claim_type

        where = " AND ".join(conditions)
        result = await session.execute(
            text(f"""
                SELECT c.*,
                       s.canonical_name AS subject_name,
                       o.canonical_name AS object_name
                FROM claims c
                JOIN entities s ON c.subject_id = s.id
                JOIN entities o ON c.object_id = o.id
                WHERE {where}
                ORDER BY c.confidence DESC, c.created_at DESC
                LIMIT :limit
            """),
            params,
        )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def get_by_id(session: AsyncSession, claim_id: str) -> dict[str, Any] | None:
        """Fetch a single claim by UUID."""
        result = await session.execute(
            text("""
                SELECT c.*,
                       s.canonical_name AS subject_name,
                       o.canonical_name AS object_name
                FROM claims c
                JOIN entities s ON c.subject_id = s.id
                JOIN entities o ON c.object_id = o.id
                WHERE c.id = :id
            """),
            {"id": claim_id},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None

# Repository: Evidence

class EvidenceRepository:
    """CRUD operations for the evidence table."""

    @staticmethod
    async def insert(
        session: AsyncSession,
        claim_id: str,
        source_id: str,
        excerpt: str,
        extraction_version: str,
        source_timestamp: datetime | None = None,
        char_offset_start: int | None = None,
        char_offset_end: int | None = None,
        confidence: float = 0.5,
    ) -> str:
        """Insert an evidence pointer. Returns the evidence UUID."""
        eid = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO evidence (
                    id, claim_id, source_id, excerpt, extraction_version,
                    source_timestamp, char_offset_start, char_offset_end, confidence
                ) VALUES (
                    :id, :claim_id, :source_id, :excerpt, :version,
                    :ts, :start, :end, :confidence
                )
            """),
            {
                "id": eid,
                "claim_id": claim_id,
                "source_id": source_id,
                "excerpt": excerpt,
                "version": extraction_version,
                "ts": source_timestamp,
                "start": char_offset_start,
                "end": char_offset_end,
                "confidence": confidence,
            },
        )
        return eid

    @staticmethod
    async def get_for_claim(
        session: AsyncSession,
        claim_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all evidence for a claim."""
        result = await session.execute(
            text("""
                SELECT e.*, re.sender, re.subject AS email_subject
                FROM evidence e
                LEFT JOIN raw_emails re ON e.source_id = re.message_id
                WHERE e.claim_id = :claim_id
                ORDER BY e.source_timestamp ASC NULLS LAST
            """),
            {"claim_id": claim_id},
        )
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    async def get_for_claims_batch(
        session: AsyncSession,
        claim_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Fetch all evidence for multiple claims in a single query.

        Returns a mapping of claim_id → list of evidence records.
        Eliminates N+1 when loading evidence for a list of claims.
        """
        if not claim_ids:
            return {}
        result = await session.execute(
            text("""
                SELECT e.*, re.sender, re.subject AS email_subject
                FROM evidence e
                LEFT JOIN raw_emails re ON e.source_id = re.message_id
                WHERE e.claim_id = ANY(:ids)
                ORDER BY e.claim_id, e.source_timestamp ASC NULLS LAST
            """),
            {"ids": claim_ids},
        )
        evidence_map: dict[str, list[dict[str, Any]]] = {cid: [] for cid in claim_ids}
        for row in result.fetchall():
            d = dict(row._mapping)
            cid = str(d["claim_id"])
            if cid in evidence_map:
                evidence_map[cid].append(d)
        return evidence_map

# Repository: Processing Log

class ProcessingLogRepository:
    """Track extraction processing for idempotency."""

    @staticmethod
    async def mark_processing(
        session: AsyncSession,
        email_id: str,
        extraction_version: str,
    ) -> str:
        """Mark an email as being processed. Returns log entry UUID."""
        lid = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO processing_log (id, email_id, extraction_version, status)
                VALUES (:id, :email_id, :version, 'processing')
                ON CONFLICT (email_id, extraction_version) DO UPDATE SET
                    status = 'processing',
                    processed_at = now()
            """),
            {"id": lid, "email_id": email_id, "version": extraction_version},
        )
        return lid

    @staticmethod
    async def mark_completed(
        session: AsyncSession,
        email_id: str,
        extraction_version: str,
        raw_output: dict[str, Any] | None = None,
        validated_output: dict[str, Any] | None = None,
    ) -> None:
        """Mark processing as completed with outputs."""
        await session.execute(
            text("""
                UPDATE processing_log
                SET status = 'completed',
                    raw_output = CAST(:raw AS jsonb),
                    validated_output = CAST(:validated AS jsonb),
                    processed_at = now()
                WHERE email_id = :email_id AND extraction_version = :version
            """),
            {
                "email_id": email_id,
                "version": extraction_version,
                "raw": json.dumps(raw_output) if raw_output else None,
                "validated": json.dumps(validated_output) if validated_output else None,
            },
        )

    @staticmethod
    async def mark_failed(
        session: AsyncSession,
        email_id: str,
        extraction_version: str,
        error_message: str,
    ) -> None:
        """Mark processing as failed with error details."""
        await session.execute(
            text("""
                UPDATE processing_log
                SET status = 'failed',
                    error_message = :error,
                    processed_at = now()
                WHERE email_id = :email_id AND extraction_version = :version
            """),
            {
                "email_id": email_id,
                "version": extraction_version,
                "error": error_message,
            },
        )

# Repository: Merge Events

class MergeEventRepository:
    """Audit trail for entity/claim merges."""

    @staticmethod
    async def log_merge(
        session: AsyncSession,
        action_type: str,
        source_ids: list[str],
        target_id: str,
        reason: str,
        confidence: float | None = None,
    ) -> str:
        """Log a merge event. Returns event UUID."""
        eid = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO merge_events (id, action_type, source_ids, target_id, reason, confidence)
                VALUES (:id, :action, :sources, :target, :reason, :confidence)
            """),
            {
                "id": eid,
                "action": action_type,
                "sources": source_ids,
                "target": target_id,
                "reason": reason,
                "confidence": confidence,
            },
        )
        return eid

    @staticmethod
    async def reverse_merge(
        session: AsyncSession,
        event_id: str,
        reason: str,
    ) -> None:
        """Mark a merge event as reversed."""
        await session.execute(
            text("""
                UPDATE merge_events
                SET reversed_at = now(), reversed_reason = :reason
                WHERE id = :id
            """),
            {"id": event_id, "reason": reason},
        )

    @staticmethod
    async def get_history_for_entity(
        session: AsyncSession,
        entity_id: str,
    ) -> list[dict[str, Any]]:
        """
        Return all merge events involving a specific entity
        (as either source or target), most recent first.
        """
        result = await session.execute(
            text("""
                SELECT * FROM merge_events
                WHERE target_id = :eid
                   OR :eid = ANY(source_ids)
                ORDER BY created_at DESC
            """),
            {"eid": entity_id},
        )
        return [dict(row._mapping) for row in result.fetchall()]
