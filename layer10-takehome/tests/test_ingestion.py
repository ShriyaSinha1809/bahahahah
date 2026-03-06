"""
Tests for the ingestion layer — email parsing and thread building.

These tests use synthetic email data to verify parsing, dedup, and
threading without requiring the actual Enron dataset.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.parse_enron import RawEmail, parse_email_file
from ingestion.thread_builder import (
    ThreadBuilder,
    normalize_subject,
    build_threads,
)
from ingestion.dedup_emails import (
    EmailDeduplicator,
    strip_quotes,
    deduplicate_emails,
)

SAMPLE_EMAIL = """\
Message-ID: <test001@example.com>
Date: Mon, 14 Jan 2002 10:00:00 -0600
From: ken.lay@enron.com
To: jeff.skilling@enron.com, sally.beck@enron.com
Cc: vince.kaminski@enron.com
Subject: Q4 Risk Review
In-Reply-To: <test000@example.com>
References: <test000@example.com>

Hi team,

Please review the attached Q4 risk assessment. We need to discuss
the VaR model assumptions before the board meeting on Friday.

Thanks,
Ken
"""

SAMPLE_EMAIL_2 = """\
Message-ID: <test002@example.com>
Date: Mon, 14 Jan 2002 11:30:00 -0600
From: jeff.skilling@enron.com
To: ken.lay@enron.com
Subject: Re: Q4 Risk Review
In-Reply-To: <test001@example.com>
References: <test000@example.com> <test001@example.com>

Ken,

I've reviewed the model. The key assumptions look solid but I think
we should revisit the correlation matrix. I'll prepare a summary.

Jeff
"""

SAMPLE_EMAIL_QUOTED = """\
Message-ID: <test003@example.com>
Date: Mon, 14 Jan 2002 14:00:00 -0600
From: sally.beck@enron.com
To: ken.lay@enron.com
Subject: Fwd: Q4 Risk Review

Here are my comments on the review.

-----Original Message-----
From: ken.lay@enron.com
Sent: Monday, January 14, 2002 10:00 AM
Subject: Q4 Risk Review

Hi team,

Please review the attached Q4 risk assessment.
"""

class TestParseEmail:
    def test_parse_valid_email(self, tmp_path: Path) -> None:
        email_file = tmp_path / "test001"
        email_file.write_text(SAMPLE_EMAIL)

        raw = parse_email_file(email_file, "lay-k/inbox")

        assert raw is not None
        assert raw.message_id == "test001@example.com"
        assert raw.sender == "ken.lay@enron.com"
        assert "jeff.skilling@enron.com" in raw.recipients
        assert "sally.beck@enron.com" in raw.recipients
        assert "vince.kaminski@enron.com" in raw.recipients
        assert raw.subject == "Q4 Risk Review"
        assert "VaR model" in raw.body
        assert raw.in_reply_to == "test000@example.com"
        assert raw.references == ["test000@example.com"]
        assert raw.folder_path == "lay-k/inbox"

    def test_parse_date(self, tmp_path: Path) -> None:
        email_file = tmp_path / "test001"
        email_file.write_text(SAMPLE_EMAIL)

        raw = parse_email_file(email_file, "test")
        assert raw is not None
        assert raw.date is not None
        assert raw.date.year == 2002
        assert raw.date.month == 1

    def test_parse_missing_message_id(self, tmp_path: Path) -> None:
        email_file = tmp_path / "bad"
        email_file.write_text("From: test@test.com\nSubject: No ID\n\nBody")

        raw = parse_email_file(email_file, "test")
        assert raw is None

    def test_body_hash_deterministic(self, tmp_path: Path) -> None:
        email_file = tmp_path / "test001"
        email_file.write_text(SAMPLE_EMAIL)

        raw = parse_email_file(email_file, "test")
        assert raw is not None
        assert raw.body_hash == raw.body_hash  # deterministic
        assert len(raw.body_hash) == 64  # SHA-256 hex

    def test_dedup_key(self, tmp_path: Path) -> None:
        email_file = tmp_path / "test001"
        email_file.write_text(SAMPLE_EMAIL)

        raw = parse_email_file(email_file, "test")
        assert raw is not None
        assert len(raw.dedup_key) == 64

def _make_raw(mid: str, subject: str, in_reply_to: str | None = None,
              refs: list[str] | None = None, sender: str = "a@test.com",
              recipients: list[str] | None = None,
              date: datetime | None = None) -> RawEmail:
    return RawEmail(
        message_id=mid,
        date=date or datetime(2002, 1, 14, tzinfo=timezone.utc),
        sender=sender,
        recipients=recipients or ["b@test.com"],
        subject=subject,
        body=f"Body of {mid}",
        in_reply_to=in_reply_to,
        references=refs or [],
        folder_path="test/inbox",
        raw_text=f"Raw text of {mid}",
    )

class TestThreadBuilder:
    def test_normalize_subject(self) -> None:
        assert normalize_subject("Re: Q4 Risk Review") == "q4 risk review"
        assert normalize_subject("Fwd: Re: FW: Hello") == "hello"
        assert normalize_subject("  Re:  spaces  ") == "spaces"

    def test_thread_by_reply_to(self) -> None:
        email_a = _make_raw("a", "Topic")
        email_b = _make_raw("b", "Re: Topic", in_reply_to="a")

        threads = build_threads([email_a, email_b])
        assert len(threads) == 1
        assert len(threads[0].email_ids) == 2

    def test_thread_by_references(self) -> None:
        email_a = _make_raw("a", "Topic")
        email_b = _make_raw("b", "Topic", refs=["a"])
        email_c = _make_raw("c", "Topic", refs=["a", "b"])

        threads = build_threads([email_a, email_b, email_c])
        assert len(threads) == 1
        assert len(threads[0].email_ids) == 3

    def test_unrelated_emails_separate_threads(self) -> None:
        email_a = _make_raw("a", "Topic A")
        email_b = _make_raw("b", "Topic B")

        threads = build_threads([email_a, email_b])
        assert len(threads) == 2

class TestDedup:
    def test_exact_dedup(self) -> None:
        email_a = _make_raw("a", "Topic", sender="x@test.com")
        # Same content, different message_id but same dedup_key
        email_b = RawEmail(
            message_id="b",
            date=email_a.date,
            sender=email_a.sender,
            recipients=email_a.recipients,
            subject=email_a.subject,
            body=email_a.body,
            in_reply_to=email_a.in_reply_to,
            references=email_a.references,
            folder_path="other/folder",
            raw_text=email_a.raw_text,
        )
        assert email_a.dedup_key == email_b.dedup_key

        result = deduplicate_emails([email_a, email_b])
        assert len(result.unique_emails) == 1
        assert len(result.events) == 1
        assert result.events[0].reason.value == "exact_duplicate"

    def test_strip_quotes(self) -> None:
        body = "Here are my comments.\n\n-----Original Message-----\nOld content here."
        stripped = strip_quotes(body)
        assert "Original Message" not in stripped
        assert "Here are my comments" in stripped

    def test_strip_angle_quotes(self) -> None:
        body = "My reply.\n> Quoted line 1\n> Quoted line 2\nMore reply."
        stripped = strip_quotes(body)
        assert "Quoted line" not in stripped
        assert "My reply" in stripped
