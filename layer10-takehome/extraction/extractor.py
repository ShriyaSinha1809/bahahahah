"""
LLM-based structured extraction from emails.

Core responsibilities:
- Call Groq (or fallback) API with structured prompts
- Async batching with rate limiting
- Retry logic with error feedback for JSON repair
- Integration with validator for post-extraction QA

Design decisions:
- Uses OpenAI-compatible API (Groq, Google, local) via the openai library.
- Rate limiter uses aiolimiter to stay within free-tier limits.
- Retries include the parse error in the follow-up message so the LLM
  can self-correct (common pattern for JSON mode failures).
- Batch processing is async but respects rate limits — throughput is
  controlled, not maximized.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from aiolimiter import AsyncLimiter
from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import get_settings
from extraction.prompts import (
    FEW_SHOT_EXAMPLES,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from extraction.schema import ExtractionResult
from extraction.validator import ValidationResult, validate_extraction
from extraction.versioning import generate_version_tag
from logging_config import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Extraction Statistics
# ──────────────────────────────────────────────────────────────


@dataclass
class ExtractionStats:
    """Tracks metrics for an extraction run."""

    total_emails: int = 0
    successful: int = 0
    failed: int = 0
    total_entities: int = 0
    total_claims: int = 0
    total_evidence_dropped: int = 0
    total_tokens_used: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def success_rate(self) -> float:
        if self.total_emails == 0:
            return 0.0
        return self.successful / self.total_emails

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_emails": self.total_emails,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate": round(self.success_rate, 3),
            "total_entities": self.total_entities,
            "total_claims": self.total_claims,
            "total_evidence_dropped": self.total_evidence_dropped,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


# ──────────────────────────────────────────────────────────────
# LLM Client Wrapper
# ──────────────────────────────────────────────────────────────


class LLMClient:
    """
    Thin wrapper around the OpenAI-compatible async client.

    Handles provider selection (Groq primary, Google fallback)
    and rate limiting.
    """

    def __init__(self) -> None:
        settings = get_settings()

        groq_client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
        )

        google_client: AsyncOpenAI | None = None
        if settings.google_api_key:
            google_client = AsyncOpenAI(
                api_key=settings.google_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )

        # Swap primary/fallback based on config flag
        if settings.use_google_primary and google_client:
            self._primary = google_client
            self._primary_model = settings.google_model
            self._fallback = groq_client
            self._fallback_model = settings.groq_model
            logger.info("llm_provider", primary="google", model=settings.google_model)
        else:
            self._primary = groq_client
            self._primary_model = settings.groq_model
            self._fallback = google_client
            self._fallback_model = settings.google_model if google_client else None
            logger.info("llm_provider", primary="groq", model=settings.groq_model)

        # Rate limiter
        self._rate_limiter = AsyncLimiter(
            max_rate=settings.extraction_rate_limit_rpm,
            time_period=60,
        )

        self._max_retries = settings.extraction_max_retries

    @property
    def model_name(self) -> str:
        return self._primary_model

    async def extract(
        self,
        messages: list[dict[str, str]],
        use_fallback: bool = False,
    ) -> tuple[str, int]:
        """
        Call the LLM and return (response_text, tokens_used).

        Retries on transient errors. Falls back to secondary provider
        if primary fails after all retries.
        """
        client = self._fallback if use_fallback and self._fallback else self._primary
        model = self._fallback_model if use_fallback and self._fallback_model else self._primary_model

        async with self._rate_limiter:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                temperature=0.1,  # Low temperature for deterministic extraction
                max_tokens=4096,
            )

        content = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if response.usage else 0
        return content, tokens


# ──────────────────────────────────────────────────────────────
# Email Data Contract (input to extractor)
# ──────────────────────────────────────────────────────────────


@dataclass
class EmailForExtraction:
    """Minimal email data needed by the extractor."""

    message_id: str
    sender: str
    recipients: list[str]
    date: str
    subject: str
    body: str
    thread_context: str | None = None


# ──────────────────────────────────────────────────────────────
# Core Extractor
# ──────────────────────────────────────────────────────────────


class Extractor:
    """
    Async LLM extraction pipeline.

    Processes emails through: prompt → LLM → validation → result.
    Supports batching and retry with error feedback.

    Usage:
        extractor = Extractor()
        result = await extractor.extract_email(email)
        # or batch:
        results = await extractor.extract_batch(emails)
    """

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()
        self._version_tag = generate_version_tag(self._client.model_name)
        self._stats = ExtractionStats()
        settings = get_settings()
        self._max_retries = settings.extraction_max_retries

    @property
    def version_tag(self) -> str:
        return self._version_tag

    @property
    def stats(self) -> ExtractionStats:
        return self._stats

    async def extract_email(
        self,
        email: EmailForExtraction,
    ) -> ValidationResult | None:
        """
        Extract entities and claims from a single email.

        Includes retry logic: if JSON parsing fails, sends the error
        back to the LLM for self-correction.
        """
        self._stats.total_emails += 1

        user_prompt = build_user_prompt(
            sender=email.sender,
            recipients=email.recipients,
            date=email.date,
            subject=email.subject,
            body=email.body,
            thread_context=email.thread_context,
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *FEW_SHOT_EXAMPLES,
            {"role": "user", "content": user_prompt},
        ]

        last_error: str | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                # Add error feedback for retry attempts
                if last_error:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response had an error: {last_error}\n"
                                "Please fix the JSON and try again. "
                                "Return ONLY valid JSON matching the schema."
                            ),
                        }
                    )

                raw_response, tokens = await self._client.extract(messages)
                self._stats.total_tokens_used += tokens

                # Validate
                result = validate_extraction(
                    raw_response=raw_response,
                    email_body=email.body,
                    email_id=email.message_id,
                )

                if result.is_valid:
                    self._stats.successful += 1
                    self._stats.total_entities += len(result.extraction.entities)
                    self._stats.total_claims += len(result.extraction.claims)
                    self._stats.total_evidence_dropped += result.dropped_count
                    return result

                # Invalid but parseable — try with error feedback
                last_error = f"Validation failed: {result.dropped_count} claims dropped"

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "extraction_attempt_failed",
                    email_id=email.message_id,
                    attempt=attempt,
                    error=last_error,
                )

                # On last retry with primary, try fallback
                if attempt == self._max_retries:
                    try:
                        raw_response, tokens = await self._client.extract(
                            messages, use_fallback=True
                        )
                        self._stats.total_tokens_used += tokens
                        result = validate_extraction(
                            raw_response, email.body, email.message_id
                        )
                        if result.is_valid:
                            self._stats.successful += 1
                            self._stats.total_entities += len(result.extraction.entities)
                            self._stats.total_claims += len(result.extraction.claims)
                            return result
                    except Exception as fallback_exc:
                        logger.error(
                            "fallback_also_failed",
                            email_id=email.message_id,
                            error=str(fallback_exc),
                        )

        self._stats.failed += 1
        logger.error(
            "extraction_failed_all_retries",
            email_id=email.message_id,
        )
        return None

    async def extract_batch(
        self,
        emails: list[EmailForExtraction],
        concurrency: int = 20,
    ) -> list[tuple[EmailForExtraction, ValidationResult | None]]:
        """
        Extract from a batch of emails with controlled concurrency.

        Uses asyncio.Semaphore to limit parallel requests (respects
        rate limiter in LLMClient, but also caps memory/connection use).
        """
        semaphore = asyncio.Semaphore(concurrency)
        results: list[tuple[EmailForExtraction, ValidationResult | None]] = []

        async def _process(em: EmailForExtraction) -> tuple[EmailForExtraction, ValidationResult | None]:
            async with semaphore:
                result = await self.extract_email(em)
                return em, result

        tasks = [_process(em) for em in emails]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.error("batch_item_exception", error=str(item))
                results.append((emails[0], None))  # placeholder
            else:
                results.append(item)

        logger.info("batch_extraction_complete", stats=self._stats.as_dict())
        return results


# ──────────────────────────────────────────────────────────────
# CLI Entrypoint
# ──────────────────────────────────────────────────────────────


async def run_extraction_pipeline() -> None:
    """
    Main extraction pipeline — reads unprocessed emails from DB,
    extracts entities/claims, and stores results.
    """
    from storage.db import (
        EntityRepository,
        ClaimRepository,
        EvidenceRepository,
        ProcessingLogRepository,
        RawEmailRepository,
        get_session,
        init_db,
    )

    await init_db()

    extractor = Extractor()
    version_tag = extractor.version_tag
    settings = get_settings()
    batch_size = settings.extraction_batch_size

    logger.info("extraction_pipeline_start", version=version_tag)

    async with get_session() as session:
        unprocessed = await RawEmailRepository.get_unprocessed(
            session, version_tag, limit=batch_size
        )

    while unprocessed:
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

        results = await extractor.extract_batch(emails)

        # Store results
        async with get_session() as session:
            for email_data, result in results:
                if result and result.is_valid:
                    # Log success
                    await ProcessingLogRepository.mark_completed(
                        session,
                        email_data.message_id,
                        version_tag,
                        raw_output=result.extraction.model_dump(),
                    )

                    # Store entities and claims
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
                            )
                else:
                    await ProcessingLogRepository.mark_failed(
                        session,
                        email_data.message_id,
                        version_tag,
                        error_message="Extraction failed or invalid",
                    )

        # Next batch
        async with get_session() as session:
            unprocessed = await RawEmailRepository.get_unprocessed(
                session, version_tag, limit=batch_size
            )

    logger.info("extraction_pipeline_complete", stats=extractor.stats.as_dict())


def main() -> None:
    """CLI entrypoint for extraction."""
    from logging_config import setup_logging

    setup_logging()
    asyncio.run(run_extraction_pipeline())


if __name__ == "__main__":
    main()
