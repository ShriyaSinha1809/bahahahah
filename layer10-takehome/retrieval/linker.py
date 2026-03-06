"""
Question → entity linking.

Maps a natural language question to candidate entities in the graph
using three complementary strategies:

1. **Keyword extraction:** Named entity recognition via regex patterns
   (lightweight, no spaCy dependency at query time).
2. **Embedding search:** Encode question → cosine similarity search in pgvector.
3. **Fuzzy name match:** Trigram similarity on entity aliases.

Results are merged and deduplicated. This is the entry point for
the retrieval pipeline.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from storage.db import EntityRepository
from storage.embeddings import search_similar_entities
from logging_config import get_logger

logger = get_logger(__name__)

# Simple heuristic: capitalized multi-word phrases are likely entity names
_NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
# Single capitalized words (might be names)
_SINGLE_NAME = re.compile(r"\b([A-Z][a-z]{2,})\b")

def extract_candidate_names(question: str) -> list[str]:
    """
    Extract potential entity names from a question.

    Uses regex heuristics rather than a full NER model to keep
    query latency low. The embedding search handles the rest.
    """
    candidates: list[str] = []

    # Multi-word names (e.g., "Kenneth Lay", "Enron Corp")
    for match in _NAME_PATTERN.finditer(question):
        candidates.append(match.group(1))

    # Single names (e.g., "Enron", "Skilling")
    for match in _SINGLE_NAME.finditer(question):
        name = match.group(1)
        if name.lower() not in {"who", "what", "when", "where", "how", "why", "did", "does", "the"}:
            candidates.append(name)

    return list(dict.fromkeys(candidates))  # dedupe preserving order

async def link_entities(
    session: AsyncSession,
    question: str,
    max_candidates: int = 10,
) -> list[dict[str, Any]]:
    """
    Map a question to candidate entities in the graph.

    Combines three retrieval strategies and deduplicates.
    """
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []

    def _add(entity: dict[str, Any], source: str) -> None:
        eid = str(entity["id"])
        if eid not in seen_ids:
            seen_ids.add(eid)
            entity["_link_source"] = source
            candidates.append(entity)

    # Strategy 1: Keyword extraction + exact/alias match
    names = extract_candidate_names(question)
    for name in names:
        matches = await EntityRepository.find_by_name(session, name)
        for m in matches:
            _add(m, "keyword_exact")

    # Strategy 2: Fuzzy name match
    for name in names:
        fuzzy = await EntityRepository.find_by_name_fuzzy(session, name, limit=3)
        for m in fuzzy:
            _add(m, "keyword_fuzzy")

    # Strategy 3: Embedding similarity
    try:
        embedding_results = await search_similar_entities(
            session, question, limit=max_candidates
        )
        for m in embedding_results:
            _add(m, "embedding")
    except Exception as exc:
        # Embedding search might fail if embeddings aren't populated yet.
        # Roll back the aborted transaction so subsequent queries can proceed.
        logger.warning("embedding_search_failed", error=str(exc))
        try:
            await session.rollback()
        except Exception:
            pass

    # Strategy 4: Fuzzy match on the full question (catches cases where
    # the question doesn't have obvious proper nouns)
    if not candidates:
        # Extract any words > 3 chars as candidates
        words = [w for w in question.split() if len(w) > 3]
        for word in words[:5]:
            fuzzy = await EntityRepository.find_by_name_fuzzy(session, word, limit=2)
            for m in fuzzy:
                _add(m, "fallback_fuzzy")

    logger.info(
        "entity_linking_complete",
        question_preview=question[:80],
        candidates=len(candidates),
    )

    return candidates[:max_candidates]
