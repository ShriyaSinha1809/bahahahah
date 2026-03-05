#!/usr/bin/env python3
"""
Run the full pipeline on real Enron maildir data.

Usage:
    python scripts/run_pipeline.py                   # full pipeline
    python scripts/run_pipeline.py --stage ingest     # just ingestion
    python scripts/run_pipeline.py --stage extract    # just extraction
    python scripts/run_pipeline.py --stage canon      # just canonicalization
    python scripts/run_pipeline.py --count            # just count emails
"""

import asyncio
import sys
import os
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings
from logging_config import setup_logging


async def count_emails():
    """Count how many emails would be ingested."""
    from ingestion.parse_enron import iter_maildir
    settings = get_settings()
    count = 0
    users_seen = set()
    for raw_email in iter_maildir(settings.enron_path, settings.enron_user_list):
        count += 1
        users_seen.add(raw_email.sender)
        if count % 1000 == 0:
            print(f"  ...counted {count} emails so far", flush=True)
    print(f"\nTotal emails: {count}")
    print(f"Unique senders: {len(users_seen)}")
    return count


async def run_ingest():
    """Stage 1: Ingest emails."""
    from pipeline import run_ingestion
    from storage.db import init_db, close_db
    await init_db()
    stored = await run_ingestion()
    await close_db()
    print(f"\n✅ Ingested {stored} emails")
    return stored


async def run_extract(max_emails: int | None = None):
    """Stage 2: LLM extraction."""
    from pipeline import run_extraction
    from storage.db import init_db, close_db
    await init_db()
    stats = await run_extraction(batch_size=max_emails or 10)
    await close_db()
    print(f"\n✅ Extraction complete: {stats}")
    return stats


async def run_canon():
    """Stage 3: Entity canonicalization."""
    from pipeline import run_canonicalization
    from storage.db import init_db, close_db
    await init_db()
    stats = await run_canonicalization()
    await close_db()
    print(f"\n✅ Canonicalization complete: {stats}")
    return stats


async def run_full():
    """All three stages."""
    from pipeline import run_full_pipeline
    results = await run_full_pipeline()
    print(f"\n✅ Full pipeline complete:")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return results


async def clear_data():
    """Clear all existing data (fresh start)."""
    from storage.db import init_db, close_db, get_session
    from sqlalchemy import text
    await init_db()
    async with get_session() as session:
        # Order matters due to foreign keys
        for table in [
            "evidence", "claims", "merge_events", "processing_log",
            "email_dedup_log", "email_threads", "entity_embeddings",
            "claim_embeddings", "extraction_configs", "source_access",
            "entities", "raw_emails"
        ]:
            await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        print("🗑️  All tables truncated")
    await close_db()


def main():
    setup_logging()
    import argparse
    parser = argparse.ArgumentParser(description="Layer10 Pipeline Runner")
    parser.add_argument("--stage", choices=["ingest", "extract", "canon", "full", "clear"], default="full")
    parser.add_argument("--count", action="store_true", help="Just count emails, don't ingest")
    parser.add_argument("--max-emails", type=int, default=None, help="Max emails per extraction batch")
    args = parser.parse_args()

    if args.count:
        asyncio.run(count_emails())
    elif args.stage == "clear":
        asyncio.run(clear_data())
    elif args.stage == "ingest":
        asyncio.run(run_ingest())
    elif args.stage == "extract":
        asyncio.run(run_extract(args.max_emails))
    elif args.stage == "canon":
        asyncio.run(run_canon())
    else:
        asyncio.run(run_full())


if __name__ == "__main__":
    main()
