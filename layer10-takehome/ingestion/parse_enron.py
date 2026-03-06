"""
Enron maildir email parser.

Walks the Enron maildir directory structure, parses each email file using
Python's stdlib `email` module, and yields structured RawEmail objects.

Design decisions:
- We use dataclasses (not Pydantic) for RawEmail because this is a data-
  transfer object in the ingestion boundary — Pydantic validation happens
  downstream when we persist.
- Parsing is intentionally lenient: malformed headers are logged and skipped
  rather than failing the entire pipeline.
- The parser supports processing a subset of users (configurable via settings)
  to stay within free-tier LLM limits during extraction.
"""

from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import AsyncIterator, Iterator

from config import get_settings
from logging_config import get_logger

logger = get_logger(__name__)

@dataclass(frozen=True, slots=True)
class RawEmail:
    """
    Immutable representation of a single parsed email.

    All fields are extracted directly from the email file; no normalization
    or deduplication is applied at this stage.
    """

    message_id: str
    date: datetime | None
    sender: str
    recipients: list[str]
    subject: str
    body: str
    in_reply_to: str | None
    references: list[str]
    folder_path: str
    raw_text: str

    @property
    def body_hash(self) -> str:
        """SHA-256 of the body text — used for exact dedup."""
        return hashlib.sha256(self.body.encode("utf-8", errors="replace")).hexdigest()

    @property
    def dedup_key(self) -> str:
        """Composite key for exact deduplication: sender + date + subject + body_hash."""
        date_str = self.date.isoformat() if self.date else "none"
        return hashlib.sha256(
            f"{self.sender}|{date_str}|{self.subject}|{self.body_hash}".encode()
        ).hexdigest()

_RE_STRIP_ANGLES = re.compile(r"<([^>]+)>")

def _parse_message_id(raw: str | None) -> str:
    """Extract a clean message-id, falling back to an empty string."""
    if not raw:
        return ""
    match = _RE_STRIP_ANGLES.search(raw)
    return match.group(1) if match else raw.strip()

def _parse_recipients(msg: email.message.Message) -> list[str]:
    """
    Merge To, Cc, Bcc into a flat list of email addresses.

    Handles comma-separated lists and RFC-2822 group syntax.
    """
    addresses: list[str] = []
    for header_name in ("To", "Cc", "Bcc"):
        raw = msg.get(header_name, "")
        if not raw:
            continue
        # Split on commas, parse each address
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            _, addr = parseaddr(chunk)
            if addr:
                addresses.append(addr.lower())
    return addresses

def _parse_date(msg: email.message.Message) -> datetime | None:
    """Parse the Date header; returns None on failure."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        return None

def _parse_references(raw: str | None) -> list[str]:
    """Parse a References header into a list of message-ids."""
    if not raw:
        return []
    return [m.group(1) for m in _RE_STRIP_ANGLES.finditer(raw)]

def _extract_body(msg: email.message.Message) -> str:
    """
    Extract the plain-text body from an email message.

    For multipart messages, walks parts and returns the first text/plain part.
    """
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
        return ""

def parse_email_file(filepath: Path, folder_path: str) -> RawEmail | None:
    """
    Parse a single email file and return a RawEmail.

    Returns None if the file is unreadable or has no message-id.
    """
    try:
        raw_text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("cannot_read_file", path=str(filepath), error=str(exc))
        return None

    msg = email.message_from_string(raw_text)

    message_id = _parse_message_id(msg.get("Message-ID"))
    if not message_id:
        logger.debug("skipping_no_message_id", path=str(filepath))
        return None

    sender_raw = msg.get("From", "")
    _, sender_addr = parseaddr(sender_raw)
    sender = sender_addr.lower() if sender_addr else sender_raw.strip().lower()

    return RawEmail(
        message_id=message_id,
        date=_parse_date(msg),
        sender=sender,
        recipients=_parse_recipients(msg),
        subject=msg.get("Subject", "").strip(),
        body=_extract_body(msg),
        in_reply_to=_parse_message_id(msg.get("In-Reply-To")),
        references=_parse_references(msg.get("References")),
        folder_path=folder_path,
        raw_text=raw_text,
    )

def iter_maildir(
    base_dir: Path,
    user_filter: list[str] | None = None,
) -> Iterator[RawEmail]:
    """
    Walk the Enron maildir and yield RawEmail objects.

    Args:
        base_dir: Path to the maildir root (e.g., data/enron_raw/maildir).
        user_filter: If provided, only process these user directories.
                     Speeds up dev iteration and respects LLM rate limits.

    Yields:
        RawEmail for each successfully parsed email file.
    """
    if not base_dir.exists():
        logger.error("maildir_not_found", path=str(base_dir))
        return

    user_dirs = sorted(base_dir.iterdir())
    if user_filter:
        user_dirs = [d for d in user_dirs if d.name in user_filter]

    total_parsed = 0
    total_skipped = 0

    for user_dir in user_dirs:
        if not user_dir.is_dir():
            continue
        logger.info("processing_user", user=user_dir.name)

        for email_file in user_dir.rglob("*"):
            if not email_file.is_file():
                continue
            # Compute relative folder path (e.g., "allen-p/inbox")
            folder_path = str(email_file.parent.relative_to(base_dir))

            raw = parse_email_file(email_file, folder_path)
            if raw:
                total_parsed += 1
                yield raw
            else:
                total_skipped += 1

    logger.info(
        "maildir_scan_complete",
        total_parsed=total_parsed,
        total_skipped=total_skipped,
    )

# Async wrapper (for pipeline compatibility)

async def aiter_maildir(
    base_dir: Path | None = None,
    user_filter: list[str] | None = None,
) -> AsyncIterator[RawEmail]:
    """
    Async wrapper around iter_maildir.

    Email parsing is CPU-bound but fast per-file; we don't need true async I/O
    here. This wrapper exists so the ingestion pipeline can be composed with
    other async stages (DB writes, LLM calls).
    """
    settings = get_settings()
    base = base_dir or settings.enron_path
    users = user_filter or settings.enron_user_list

    for raw_email in iter_maildir(base, users):
        yield raw_email

def main() -> None:
    """Parse Enron emails and print summary stats (CLI entrypoint)."""
    from logging_config import setup_logging

    setup_logging()

    settings = get_settings()
    base = settings.enron_path
    users = settings.enron_user_list

    logger.info("starting_parse", base_dir=str(base), users=users)

    seen_ids: set[str] = set()
    dedup_count = 0
    total = 0

    for raw_email in iter_maildir(base, users):
        total += 1
        if raw_email.dedup_key in seen_ids:
            dedup_count += 1
        else:
            seen_ids.add(raw_email.dedup_key)

    unique = total - dedup_count
    logger.info(
        "parse_complete",
        total_emails=total,
        unique_emails=unique,
        exact_duplicates=dedup_count,
    )

if __name__ == "__main__":
    main()
