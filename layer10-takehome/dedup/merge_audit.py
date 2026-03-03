"""
Merge audit trail management.

Provides functions to log, query, and reverse merge events
in the database. Every entity merge, claim merge, and artifact
dedup action flows through this module.

This is the foundation of reversibility: if an entity merge was
wrong, we can undo it by restoring the original entities and
re-linking their evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from logging_config import get_logger

logger = get_logger(__name__)


async def log_entity_merge(
    session: AsyncSession,
    source_ids: list[str],
    target_id: str,
    reason: str,
    confidence: float,
) -> str:
    """
    Log an entity merge event.

    Returns the merge event UUID.
    """
    from storage.db import MergeEventRepository

    event_id = await MergeEventRepository.log_merge(
        session,
        action_type="entity_merge",
        source_ids=source_ids,
        target_id=target_id,
        reason=reason,
        confidence=confidence,
    )
    logger.info(
        "entity_merge_logged",
        event_id=event_id,
        source_count=len(source_ids),
        target_id=target_id,
        reason=reason,
    )
    return event_id


async def log_claim_merge(
    session: AsyncSession,
    source_ids: list[str],
    target_id: str,
    reason: str,
) -> str:
    """Log a claim merge event."""
    from storage.db import MergeEventRepository

    event_id = await MergeEventRepository.log_merge(
        session,
        action_type="claim_merge",
        source_ids=source_ids,
        target_id=target_id,
        reason=reason,
    )
    logger.info(
        "claim_merge_logged",
        event_id=event_id,
        source_count=len(source_ids),
        target_id=target_id,
    )
    return event_id


async def reverse_merge(
    session: AsyncSession,
    event_id: str,
    reason: str,
) -> None:
    """
    Reverse a merge event.

    This marks the event as reversed in the audit log.
    The actual restoration of entities/claims must be handled
    by the caller (re-create split entities, re-link evidence).
    """
    from storage.db import MergeEventRepository

    await MergeEventRepository.reverse_merge(session, event_id, reason)
    logger.info("merge_reversed", event_id=event_id, reason=reason)


async def get_merge_history(
    session: AsyncSession,
    entity_id: str,
) -> list[dict[str, Any]]:
    """
    Get all merge events involving a specific entity.

    Returns events where the entity was either a source or target.
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
