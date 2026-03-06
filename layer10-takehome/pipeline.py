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
from datetime import datetime
from typing import Any

from config import get_settings
from ingestion.dedup_emails import deduplicate_emails
from ingestion.parse_enron import RawEmail, iter_maildir
from ingestion.signal_filter import filter_emails
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
from storage.embeddings import store_entity_embeddings
from logging_config import get_logger, setup_logging

logger = get_logger(__name__)

async def run_ingestion() -> int:
    """
    Parse Enron emails, deduplicate, and store in raw_emails table.

    Returns count of unique emails stored.
    """
    settings = get_settings()
    logger.info("ingestion_start", data_dir=str(settings.enron_path))

    raw_emails = list(iter_maildir(settings.enron_path, settings.enron_user_list))
    logger.info("parsing_complete", total_parsed=len(raw_emails))

    filtered_emails, filter_stats = filter_emails(raw_emails)
    logger.info("signal_filter_complete", **filter_stats)

    dedup_result = deduplicate_emails(filtered_emails)
    unique_emails = dedup_result.unique_emails
    logger.info("dedup_complete", unique=len(unique_emails), stats=dedup_result.stats)

    threads = build_threads(unique_emails)
    logger.info("threads_built", thread_count=len(threads))

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
                            # Invalidate any conflicting current claim for
                            # mutually-exclusive types (WORKS_AT, REPORTS_TO)
                            # before inserting the new one, closing the old
                            # claim's validity window.
                            email_date: datetime | None = None
                            _date_str = email_data.date
                            if _date_str:
                                try:
                                    email_date = datetime.fromisoformat(
                                        _date_str.replace("Z", "+00:00")
                                    )
                                except ValueError:
                                    email_date = None
                            await ClaimRepository.invalidate_conflicting(
                                session,
                                subject_id=subj_id,
                                claim_type=claim.type.value,
                                new_valid_from=email_date,
                            )
                            # Claims with confidence below 0.5 are stored but
                            # flagged for human review.
                            pending = claim.confidence < 0.5
                            cid = await ClaimRepository.insert(
                                session,
                                claim_type=claim.type.value,
                                subject_id=subj_id,
                                object_id=obj_id,
                                properties=claim.properties,
                                confidence=claim.confidence,
                                pending_review=pending,
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

    logger.info("pipeline_stage", stage="embeddings")
    try:
        async with get_session() as session:
            all_entities = await EntityRepository.list_all(session, limit=100_000)
        async with get_session() as session:
            embed_count = await store_entity_embeddings(session, all_entities)
        logger.info("embeddings_stored", count=embed_count)
    except Exception as e:
        logger.warning("embeddings_skipped", reason=str(e))

    return stats

async def run_claim_dedup() -> dict[str, int]:
    """
    Deduplicate claims with identical (subject, type, object) keys.

    Where multiple claims assert the same relationship, keep the one
    with the highest confidence as is_current=true and mark others
    superseded. All evidence pointers are retained on the canonical claim.

    Returns counts of examined and merged claims.
    """
    logger.info("claim_dedup_start")

    async with get_session() as session:
        # Find duplicate groups: same subject+type+object with >1 claim
        result = await session.execute(
            __import__("sqlalchemy").text("""
                WITH dupes AS (
                    SELECT subject_id, claim_type, object_id,
                           COUNT(*) AS cnt,
                           MAX(confidence) AS max_conf,
                           MIN(id::text) AS first_id
                    FROM claims
                    WHERE is_current = true
                    GROUP BY subject_id, claim_type, object_id
                    HAVING COUNT(*) > 1
                )
                SELECT c.id::text AS claim_id,
                       c.confidence,
                       d.max_conf,
                       c.subject_id::text,
                       c.claim_type,
                       c.object_id::text
                FROM claims c
                JOIN dupes d
                    ON c.subject_id = d.subject_id
                   AND c.claim_type = d.claim_type
                   AND c.object_id  = d.object_id
                WHERE c.is_current = true
                ORDER BY c.subject_id, c.claim_type, c.object_id, c.confidence DESC
            """)
        )
        rows = [dict(r._mapping) for r in result.fetchall()]

    # Group and process
    from itertools import groupby
    from storage.db import MergeEventRepository

    merged = 0
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (row["subject_id"], row["claim_type"], row["object_id"])
        groups.setdefault(key, []).append(row)

    async with get_session() as session:
        for (subj, ctype, obj), group in groups.items():
            if len(group) <= 1:
                continue
            # group is sorted DESC by confidence: first is canonical
            canonical_id = group[0]["claim_id"]
            superseded_ids = [g["claim_id"] for g in group[1:]]

            # Mark superseded claims
            await session.execute(
                __import__("sqlalchemy").text("""
                    UPDATE claims
                    SET is_current = false, valid_to = now()
                    WHERE id = ANY(:ids::uuid[])
                """),
                {"ids": superseded_ids},
            )

            # Re-attach their evidence to canonical claim
            await session.execute(
                __import__("sqlalchemy").text("""
                    UPDATE evidence
                    SET claim_id = :canonical::uuid
                    WHERE claim_id = ANY(:superseded::uuid[])
                """),
                {"canonical": canonical_id, "superseded": superseded_ids},
            )

            # Log merge event
            await MergeEventRepository.log_merge(
                session,
                action_type="claim_merge",
                source_ids=superseded_ids,
                target_id=canonical_id,
                reason=f"same_semantic_key:{ctype}",
                confidence=group[0]["confidence"],
            )
            merged += len(superseded_ids)

    stats = {"examined_groups": len(groups), "merged_claims": merged}
    logger.info("claim_dedup_complete", **stats)
    return stats

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

async def run_full_pipeline() -> dict[str, Any]:
    """
    Run the complete pipeline end-to-end.

    Stages: ingest → extract → canonicalize
    Each stage is independently resumable.
    """
    await init_db()

    results: dict[str, Any] = {}

    logger.info("pipeline_stage", stage="ingestion")
    results["ingestion_count"] = await run_ingestion()

    logger.info("pipeline_stage", stage="extraction")
    results["extraction_stats"] = await run_extraction()

    logger.info("pipeline_stage", stage="canonicalization")
    results["canonicalization_stats"] = await run_canonicalization()

    logger.info("pipeline_stage", stage="claim_dedup")
    results["claim_dedup_stats"] = await run_claim_dedup()

    await close_db()

    logger.info("full_pipeline_complete", results=results)
    return results

def main() -> None:
    """CLI entrypoint for the full pipeline."""
    setup_logging()
    asyncio.run(run_full_pipeline())

if __name__ == "__main__":
    main()
