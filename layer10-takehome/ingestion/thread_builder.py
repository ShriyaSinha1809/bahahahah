"""
Email thread reconstruction.

Reconstructs conversation threads from individual emails using three
signals, in priority order:

1. **In-Reply-To header:** Direct parent link — most reliable.
2. **References header:** Ordered list of ancestors — fills gaps when
   In-Reply-To is missing.
3. **Subject-line fallback:** Strip Re:/Fwd: prefixes, group by
   normalized subject within a time window and participant overlap.

Design decisions:
- We use Union-Find (disjoint set) for efficient thread merging.
- Subject-based matching is conservative: requires both a time window
  (≤7 days) AND participant overlap to avoid false thread merges.
- Thread assignment is deterministic and idempotent — same inputs always
  produce the same thread IDs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from ingestion.parse_enron import RawEmail
from logging_config import get_logger

logger = get_logger(__name__)

@dataclass
class EmailThread:
    """A reconstructed email conversation thread."""

    thread_id: str
    email_ids: list[str] = field(default_factory=list)
    subject: str = ""
    earliest: datetime | None = None
    latest: datetime | None = None
    participants: set[str] = field(default_factory=set)

    def add_email(self, raw: RawEmail) -> None:
        """Add an email to this thread, updating metadata."""
        self.email_ids.append(raw.message_id)
        self.participants.add(raw.sender)
        self.participants.update(raw.recipients)

        if raw.date:
            if self.earliest is None or raw.date < self.earliest:
                self.earliest = raw.date
            if self.latest is None or raw.date > self.latest:
                self.latest = raw.date

        if not self.subject and raw.subject:
            self.subject = raw.subject

class UnionFind:
    """Disjoint-set with path compression and union by rank."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # path compression
        return self._parent[x]

    def union(self, a: str, b: str) -> str:
        """Merge sets containing a and b. Returns the new root."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        # Union by rank
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        return ra

_RE_PREFIX = re.compile(r"^(?:(?:re|fwd?|fw)\s*:\s*)+", re.IGNORECASE)

def normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes and normalize whitespace."""
    cleaned = _RE_PREFIX.sub("", subject).strip()
    return " ".join(cleaned.split()).lower()

# Time window for subject-based fallback matching
_SUBJECT_WINDOW = timedelta(days=7)
# Minimum participant overlap ratio for subject-based matching
_MIN_PARTICIPANT_OVERLAP = 0.3

class ThreadBuilder:
    """
    Builds email threads from a collection of RawEmails.

    Usage:
        builder = ThreadBuilder()
        for email in emails:
            builder.add(email)
        threads = builder.build()
    """

    def __init__(self) -> None:
        self._emails: dict[str, RawEmail] = {}
        self._uf = UnionFind()
        # For subject-based fallback
        self._by_subject: dict[str, list[str]] = defaultdict(list)

    def add(self, raw: RawEmail) -> None:
        """Register an email for thread building."""
        self._emails[raw.message_id] = raw

    def add_all(self, emails: Iterable[RawEmail]) -> None:
        """Register a batch of emails."""
        for email in emails:
            self.add(email)

    def build(self) -> list[EmailThread]:
        """
        Execute thread reconstruction and return built threads.

        Three-pass algorithm:
        1. Link via In-Reply-To headers
        2. Link via References headers
        3. Subject-based fallback with time + participant constraints
        """
        logger.info("thread_build_start", email_count=len(self._emails))

        # Pass 1: In-Reply-To
        for mid, raw in self._emails.items():
            if raw.in_reply_to and raw.in_reply_to in self._emails:
                self._uf.union(mid, raw.in_reply_to)

        # Pass 2: References (chain of ancestors)
        for mid, raw in self._emails.items():
            for ref in raw.references:
                if ref in self._emails:
                    self._uf.union(mid, ref)

        # Pass 3: Subject-based fallback (conservative)
        self._subject_fallback()

        # Collect threads
        thread_map: dict[str, EmailThread] = {}
        for mid, raw in self._emails.items():
            root = self._uf.find(mid)
            if root not in thread_map:
                thread_map[root] = EmailThread(thread_id=root)
            thread_map[root].add_email(raw)

        threads = list(thread_map.values())
        logger.info(
            "thread_build_complete",
            total_threads=len(threads),
            avg_size=round(len(self._emails) / max(len(threads), 1), 1),
        )
        return threads

    def _subject_fallback(self) -> None:
        """
        Group emails by normalized subject, then merge those within
        the time window that share participants.

        This is intentionally conservative to avoid merging unrelated
        conversations that happen to share a common subject line
        (e.g., "Meeting tomorrow").
        """
        # Build subject index — only include emails not yet linked
        subject_groups: dict[str, list[str]] = defaultdict(list)
        for mid, raw in self._emails.items():
            if raw.subject:
                norm = normalize_subject(raw.subject)
                if norm:
                    subject_groups[norm].append(mid)

        for _subject, mids in subject_groups.items():
            if len(mids) < 2:
                continue

            # Sort by date for time-window check
            dated = [
                (mid, self._emails[mid])
                for mid in mids
                if self._emails[mid].date is not None
            ]
            dated.sort(key=lambda x: x[1].date)  # type: ignore[arg-type]

            for i in range(len(dated)):
                mid_a, raw_a = dated[i]
                parts_a = {raw_a.sender} | set(raw_a.recipients)

                for j in range(i + 1, len(dated)):
                    mid_b, raw_b = dated[j]

                    # Time window check
                    assert raw_a.date is not None and raw_b.date is not None
                    if raw_b.date - raw_a.date > _SUBJECT_WINDOW:
                        break  # sorted, so all subsequent are farther

                    # Participant overlap check
                    parts_b = {raw_b.sender} | set(raw_b.recipients)
                    overlap = len(parts_a & parts_b)
                    union = len(parts_a | parts_b)
                    if union > 0 and overlap / union >= _MIN_PARTICIPANT_OVERLAP:
                        # Already in same thread? Skip.
                        if self._uf.find(mid_a) != self._uf.find(mid_b):
                            self._uf.union(mid_a, mid_b)

def build_threads(emails: Iterable[RawEmail]) -> list[EmailThread]:
    """One-shot thread builder for a collection of emails."""
    builder = ThreadBuilder()
    builder.add_all(emails)
    return builder.build()

def main() -> None:
    """Build threads from parsed emails (CLI entrypoint)."""
    from logging_config import setup_logging

    setup_logging()

    from ingestion.parse_enron import iter_maildir

    settings = get_settings()
    emails = list(iter_maildir(settings.enron_path, settings.enron_user_list))

    threads = build_threads(emails)
    sizes = [len(t.email_ids) for t in threads]
    logger.info(
        "thread_stats",
        total_threads=len(threads),
        max_thread_size=max(sizes) if sizes else 0,
        avg_thread_size=round(sum(sizes) / len(sizes), 1) if sizes else 0,
    )

if __name__ == "__main__":
    from config import get_settings

    main()
