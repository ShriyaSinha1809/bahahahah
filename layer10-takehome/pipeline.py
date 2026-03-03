"""
End-to-end pipeline orchestrator.

Composes the full flow:
  Email files → Parse → Dedup → Store raw → Extract → Validate
  → Canonicalize → Insert entities → Insert claims → Insert evidence

Design decisions:
- The pipeline is resumable: it checks processing_log for already-done
  emails and skips them (idempotent by extraction version).
- Each stage logs metrics so you can see bottlenecks.
- Errors in one email don't stop the batch — logged and continued.
- Batch sizes are configurable to respect memory and API limits.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from config import get_settings
from ingestion.dedup_emails import deduplicate_emails
from ingestion.parse_enron import RawEmail, iter_maildir
from ingestion.thread_builder import build_threads
from extraction.extractor import EmailForExtraction, Extractor
from extraction.versioning import generate_version_tag
from storage.db import (
    ClaimRepository,
    EntityRepository,
    EvidenceRepository,
    ProcessingLogRepository,
    RawEmailRepository,
    get_session,
    init_db,
    close_db,
)
from logging_config import get_logger, setup_logging

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Stage 1: Ingest raw emails into database
# ──────────────────────────────────────────────────────────────


async def run_ingestion() -> int:
    """
    Parse Enron emails, deduplicate, and store in raw_emails table.

    Returns count of unique emails stored.
    """
    settings = get_settings()
    logger.info("ingestion_start", data_dir=str(settings.enron_path))

    # Parse
    raw_emails = list(iter_maildir(settings.enron_path, settings.enron_user_list))
    logger.info("parsing_complete", total_parsed=len(raw_emails))

    # Dedup
    dedup_result = deduplicate_emails(raw_emails)
    unique_emails = dedup_result.unique_emails
    logger.info("dedup_complete", unique=len(unique_emails), stats=dedup_result.stats)

    # Build threads (for later context in extraction)
    threads = build_threads(unique_emails)
    logger.info("threads_built", thread_count=len(threads))

    # Store raw emails
    batch_size = 500
    stored = 0

    for i in range(0, len(unique_emails), batch_size):
        batch = unique_emails[i : i + batch_size]
        email_dicts = [
            {
                "message_id": em.message_id,
                "sender": em.sender,
                "recipients": em.recipients,
                "subject": em.subject,
                "body": em.body,
                "date": em.date,
                "in_reply_to": em.in_reply_to,
                "references": em.references,
                "folder_path": em.folder_path,
                "raw_text": em.raw_text,
                "body_hash": em.body_hash,
                "dedup_key": em.dedup_key,
            }
            for em in batch
        ]
        async with get_session() as session:
            count = await RawEmailRepository.upsert_batch(session, email_dicts)
            stored += count

    # Store threads
    async with get_session() as session:
        for thread in threads:
            await session.execute(
                __import__("sqlalchemy").text("""
                    INSERT INTO email_threads (thread_id, subject, email_ids, participant_count, earliest, latest)
                    VALUES (:tid, :subject, :eids, :pcount, :earliest, :latest)
                    ON CONFLICT (thread_id) DO NOTHING
                """),
                {
                    "tid": thread.thread_id,
                    "subject": thread.subject,
                    "eids": thread.email_ids,
                    "pcount": len(thread.participants),
                    "earliest": thread.earliest,
                    "latest": thread.latest,
                },
            )

    # Store dedup log
    async with get_session() as session:
        for event in dedup_result.events:
            await session.execute(
                __import__("sqlalchemy").text("""
                    INSERT INTO email_dedup_log (source_id, canonical_id, reason, similarity)
                    VALUES (:src, :canonical, :reason, :sim)
                """),
                {
                    "src": event.source_id,
                    "canonical": event.canonical_id,
                    "reason": event.reason.value,
                    "sim": event.similarity,
                },
            )

    logger.info("ingestion_complete", stored=stored)
    return stored


# ──────────────────────────────────────────────────────────────
# Stage 2: LLM extraction
# ──────────────────────────────────────────────────────────────


async def run_extraction(batch_size: int | None = None) -> dict[str, Any]:
    """
    Run LLM extraction on unprocessed emails.

    Fetches unprocessed emails, sends them through the extraction
    pipeline, validates results, and stores entities/claims/evidence.

    Returns extraction statistics.
    """
    settings = get_settings()
    bs = batch_size or settings.extraction_batch_size

    extractor = Extractor()
    version_tag = extractor.version_tag
    logger.info("extraction_start", version=version_tag, batch_size=bs)

    processed_total = 0

    while True:
        # Fetch next batch of unprocessed emails
        async with get_session() as session:
            unprocessed = await RawEmailRepository.get_unprocessed(
                session, version_tag, limit=bs
            )

        if not unprocessed:
            break

        # Convert to extraction format
        emails = [
            EmailForExtraction(
                message_id=row["message_id"],
                sender=row["sender"],
                recipients=row.get("recipients", []),
                date=str(row.get("date", "")),
                subject=row.get("subject", ""),
                body=row.get("body", ""),
            )
            for row in unprocessed
        ]

        # Mark as processing
        async with get_session() as session:
            for em in emails:
                await ProcessingLogRepository.mark_processing(
                    session, em.message_id, version_tag
                )

        # Extract
        results = await extractor.extract_batch(emails)

        # Store results
        async with get_session() as session:
            for email_data, result in results:
                if result and result.is_valid:
                    # Store entities
                    entity_id_map: dict[str, str] = {}
                    for entity in result.extraction.entities:
                        eid = await EntityRepository.upsert(
                            session,
                            canonical_name=entity.name,
                            entity_type=entity.type.value,
                            aliases=entity.aliases,
                            properties=entity.properties,
                        )
                        entity_id_map[entity.name.lower()] = eid
                        for alias in entity.aliases:
                            entity_id_map[alias.lower()] = eid

                    # Store claims + evidence
                    for claim in result.extraction.claims:
                        subj_id = entity_id_map.get(claim.subject.lower())
                        obj_id = entity_id_map.get(claim.object.lower())
                        if subj_id and obj_id:
                            cid = await ClaimRepository.insert(
                                session,
                                claim_type=claim.type.value,
                                subject_id=subj_id,
                                object_id=obj_id,
                                properties=claim.properties,
                                confidence=claim.confidence,
                            )
                            await EvidenceRepository.insert(
                                session,
                                claim_id=cid,
                                source_id=email_data.message_id,
                                excerpt=claim.evidence_excerpt,
                                extraction_version=version_tag,
                                confidence=claim.confidence,
                                source_timestamp=None,  # Could parse from email
                            )

                    await ProcessingLogRepository.mark_completed(
                        session,
                        email_data.message_id,
                        version_tag,
                        raw_output=result.extraction.model_dump(),
                    )
                else:
                    await ProcessingLogRepository.mark_failed(
                        session,
                        email_data.message_id,
                        version_tag,
                        error_message="Extraction failed or invalid",
                    )

        processed_total += len(results)
        logger.info("batch_stored", processed=processed_total)

    stats = extractor.stats.as_dict()
    logger.info("extraction_complete", **stats)
    return stats


# ──────────────────────────────────────────────────────────────
# Stage 3: Entity canonicalization
# ──────────────────────────────────────────────────────────────


async def run_canonicalization() -> dict[str, int]:
    """
    Run entity resolution on all stored entities.

    Merges duplicate entities, updates claims to point to canonical
    entities, and logs all merge events.
    """
    from dedup.entity_resolver import resolve_entities
    from dedup.merge_audit import log_entity_merge

    logger.info("canonicalization_start")

    # Load all entities
    async with get_session() as session:
        entities = await EntityRepository.list_all(session, limit=100_000)

    if not entities:
        return {"canonical": 0, "merged": 0}

    # Resolve
    canonical, merge_events = resolve_entities(entities)

    # Persist merge events
    async with get_session() as session:
        for event in merge_events:
            await log_entity_merge(
                session,
                source_ids=[event.merged_from],
                target_id=event.merged_into,
                reason=event.reason,
                confidence=event.confidence,
            )

    stats = {"canonical": len(canonical), "merged": len(merge_events)}
    logger.info("canonicalization_complete", **stats)
    return stats


# ──────────────────────────────────────────────────────────────
# Full Pipeline
# ──────────────────────────────────────────────────────────────


async def run_full_pipeline() -> dict[str, Any]:
    """
    Run the complete pipeline end-to-end.

    Stages: ingest → extract → canonicalize
    Each stage is independently resumable.
    """
    await init_db()

    results: dict[str, Any] = {}

    # Stage 1
    logger.info("pipeline_stage", stage="ingestion")
    results["ingestion_count"] = await run_ingestion()

    # Stage 2
    logger.info("pipeline_stage", stage="extraction")
    results["extraction_stats"] = await run_extraction()

    # Stage 3
    logger.info("pipeline_stage", stage="canonicalization")
    results["canonicalization_stats"] = await run_canonicalization()

    await close_db()

    logger.info("full_pipeline_complete", results=results)
    return results


def main() -> None:
    """CLI entrypoint for the full pipeline."""
    setup_logging()
    asyncio.run(run_full_pipeline())


if __name__ == "__main__":
    main()
