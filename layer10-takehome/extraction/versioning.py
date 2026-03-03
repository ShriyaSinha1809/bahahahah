"""
Extraction version management.

Generates and tracks versioned extraction configurations so that:
- Every extraction run is tagged for reproducibility.
- Schema/prompt changes trigger re-extraction of affected emails.
- Old extractions are preserved (soft-deleted, not overwritten).

Version tag format: v{schema}_{model}_{prompt_hash}_{timestamp}
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from extraction.prompts import SYSTEM_PROMPT, get_prompt_hash
from config import get_settings
from logging_config import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = "v1.0"


def generate_version_tag(model_name: str | None = None) -> str:
    """
    Generate a deterministic version tag for the current extraction config.

    Same prompt + same model + same schema → same tag.
    This ensures idempotency: re-running with identical config skips
    already-processed emails.
    """
    settings = get_settings()
    model = model_name or settings.groq_model
    prompt_h = get_prompt_hash()
    return f"{SCHEMA_VERSION}_{model}_{prompt_h}"


def generate_run_tag(model_name: str | None = None) -> str:
    """
    Generate a unique run tag (includes timestamp).

    Used for logging and tracing individual runs, not for idempotency.
    """
    base = generate_version_tag(model_name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{base}_{ts}"
