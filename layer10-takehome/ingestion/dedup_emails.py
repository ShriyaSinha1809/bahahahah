"""
Email-level artifact deduplication.

Handles three levels of dedup:

1. **Exact dedup:** Hash of (sender, date, subject, body_hash) — catches
   true duplicates that appear in multiple folders.
2. **Quote stripping:** Detects forwarded/quoted blocks and extracts the
   "new" content for downstream extraction.
3. **Near-dedup:** MinHash/SimHash on body text — flags clusters with
   Jaccard > 0.85 and keeps the earliest as canonical.

Design decisions:
- All dedup decisions are logged to a DedupEvent list for audit and
  reversibility. Nothing is silently dropped.
- Quote stripping is aggressive but stores the original — extraction
  prompts receive the stripped body but can reference the full raw_text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Iterator

from datasketch import MinHash, MinHashLSH

from ingestion.parse_enron import RawEmail
from logging_config import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Dedup Event Model (audit trail)
# ──────────────────────────────────────────────────────────────


class DedupReason(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    QUOTE_STRIPPED = "quote_stripped"


@dataclass(frozen=True, slots=True)
class DedupEvent:
    """Audit record for every dedup decision."""

    source_id: str
    canonical_id: str
    reason: DedupReason
    similarity: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────
# Quote / Forward Stripping
# ──────────────────────────────────────────────────────────────

_QUOTE_PATTERNS = [
    re.compile(r"^>+\s?.*$", re.MULTILINE),  # > quoted lines
    re.compile(
        r"-{3,}\s*Original Message\s*-{3,}.*",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"-{3,}\s*Forwarded by\s.*?-{3,}.*",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"On .{10,80} wrote:\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
]


def strip_quotes(body: str) -> str:
    """
    Remove quoted/forwarded content from an email body.

    Returns the "new" content the sender actually wrote. The original
    body is preserved in RawEmail.raw_text for evidence pointing.
    """
    stripped = body
    for pattern in _QUOTE_PATTERNS:
        stripped = pattern.sub("", stripped)
    # Collapse blank lines
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped


# ──────────────────────────────────────────────────────────────
# MinHash Helpers
# ──────────────────────────────────────────────────────────────

_NUM_PERM = 128  # MinHash permutations — good balance of speed vs accuracy


def _text_to_shingles(text: str, k: int = 5) -> set[str]:
    """Convert text into a set of character k-shingles."""
    text = text.lower().strip()
    if len(text) < k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _make_minhash(text: str) -> MinHash:
    """Create a MinHash signature for a text."""
    mh = MinHash(num_perm=_NUM_PERM)
    for shingle in _text_to_shingles(text):
        mh.update(shingle.encode("utf-8"))
    return mh


# ──────────────────────────────────────────────────────────────
# Deduplicator
# ──────────────────────────────────────────────────────────────


@dataclass
class DedupResult:
    """Result of the deduplication pass."""

    unique_emails: list[RawEmail]
    events: list[DedupEvent]
    stats: dict[str, int]


class EmailDeduplicator:
    """
    Multi-level email deduplicator.

    Usage:
        deduper = EmailDeduplicator()
        result = deduper.deduplicate(raw_emails)
    """

    def __init__(
        self,
        near_dedup_threshold: float = 0.85,
    ) -> None:
        self._threshold = near_dedup_threshold
        self._events: list[DedupEvent] = []

    def deduplicate(self, emails: Iterable[RawEmail]) -> DedupResult:
        """
        Run full dedup pipeline:
        1. Exact dedup by composite hash
        2. Near-dedup via MinHash LSH
        """
        all_emails = list(emails)
        logger.info("dedup_start", input_count=len(all_emails))

        # Phase 1: Exact dedup
        exact_unique, exact_events = self._exact_dedup(all_emails)
        self._events.extend(exact_events)

        # Phase 2: Near-dedup
        final_unique, near_events = self._near_dedup(exact_unique)
        self._events.extend(near_events)

        stats = {
            "input": len(all_emails),
            "after_exact_dedup": len(exact_unique),
            "after_near_dedup": len(final_unique),
            "exact_dups_removed": len(all_emails) - len(exact_unique),
            "near_dups_removed": len(exact_unique) - len(final_unique),
        }
        logger.info("dedup_complete", **stats)

        return DedupResult(
            unique_emails=final_unique,
            events=self._events,
            stats=stats,
        )

    def _exact_dedup(
        self, emails: list[RawEmail]
    ) -> tuple[list[RawEmail], list[DedupEvent]]:
        """Remove exact duplicates using composite hash key."""
        seen: dict[str, RawEmail] = {}
        events: list[DedupEvent] = []

        for em in emails:
            key = em.dedup_key
            if key in seen:
                events.append(
                    DedupEvent(
                        source_id=em.message_id,
                        canonical_id=seen[key].message_id,
                        reason=DedupReason.EXACT_DUPLICATE,
                        similarity=1.0,
                    )
                )
            else:
                seen[key] = em

        return list(seen.values()), events

    def _near_dedup(
        self, emails: list[RawEmail]
    ) -> tuple[list[RawEmail], list[DedupEvent]]:
        """Remove near-duplicates using MinHash LSH."""
        if len(emails) < 2:
            return emails, []

        # Build LSH index
        lsh = MinHashLSH(threshold=self._threshold, num_perm=_NUM_PERM)
        minhashes: dict[str, MinHash] = {}
        email_map: dict[str, RawEmail] = {}

        for em in emails:
            mh = _make_minhash(em.body)
            minhashes[em.message_id] = mh
            email_map[em.message_id] = em
            try:
                lsh.insert(em.message_id, mh)
            except ValueError:
                # Duplicate key in LSH — skip
                pass

        # Find clusters
        seen: set[str] = set()
        unique: list[RawEmail] = []
        events: list[DedupEvent] = []

        # Sort by date (earliest first) so we keep the canonical = earliest
        sorted_emails = sorted(
            emails,
            key=lambda e: e.date or datetime.min,
        )

        for em in sorted_emails:
            if em.message_id in seen:
                continue

            # This email is the canonical for its cluster
            seen.add(em.message_id)
            unique.append(em)

            # Find near-duplicates
            candidates = lsh.query(minhashes[em.message_id])
            for cand_id in candidates:
                if cand_id == em.message_id or cand_id in seen:
                    continue
                # Verify Jaccard similarity
                sim = minhashes[em.message_id].jaccard(minhashes[cand_id])
                if sim >= self._threshold:
                    seen.add(cand_id)
                    events.append(
                        DedupEvent(
                            source_id=cand_id,
                            canonical_id=em.message_id,
                            reason=DedupReason.NEAR_DUPLICATE,
                            similarity=sim,
                        )
                    )

        return unique, events


# ──────────────────────────────────────────────────────────────
# Convenience
# ──────────────────────────────────────────────────────────────


def deduplicate_emails(
    emails: Iterable[RawEmail],
    threshold: float = 0.85,
) -> DedupResult:
    """One-shot deduplication for a collection of emails."""
    return EmailDeduplicator(near_dedup_threshold=threshold).deduplicate(emails)
