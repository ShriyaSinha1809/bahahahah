"""
Embedding generation and storage.

Generates vector embeddings for entities and claims using
sentence-transformers, then stores them in pgvector for
semantic similarity search.

Design decisions:
- Uses all-MiniLM-L6-v2 (384 dimensions) — good quality/speed tradeoff
  and runs locally without API calls.
- Embeddings are generated from a rich text representation that includes
  entity name, type, aliases, and relationship context — not just the name.
- Batch processing for efficiency with configurable batch sizes.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from logging_config import get_logger

logger = get_logger(__name__)

_model = None

def _get_model():
    """Lazy-load the sentence transformer model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        settings = get_settings()
        _model = SentenceTransformer(settings.embedding_model)
        logger.info("embedding_model_loaded", model=settings.embedding_model)
    return _model

def entity_to_text(entity: dict[str, Any]) -> str:
    """
    Build a rich text representation of an entity for embedding.

    Includes name, type, aliases, and key properties to produce
    a semantically meaningful vector.
    """
    parts = [
        entity.get("canonical_name", ""),
        f"({entity.get('entity_type', '')})",
    ]
    aliases = entity.get("aliases", [])
    if aliases:
        parts.append(f"also known as: {', '.join(aliases)}")

    props = entity.get("properties", {})
    if isinstance(props, dict):
        for key in ("role", "title", "department", "description"):
            if key in props:
                parts.append(f"{key}: {props[key]}")

    return " ".join(parts)

def claim_to_text(claim: dict[str, Any]) -> str:
    """Build a text representation of a claim for embedding."""
    parts = [
        claim.get("subject_name", ""),
        claim.get("claim_type", ""),
        claim.get("object_name", ""),
    ]
    props = claim.get("properties", {})
    if isinstance(props, dict):
        for key, value in props.items():
            parts.append(f"{key}: {value}")
    return " ".join(parts)

def generate_embeddings(texts: list[str]) -> np.ndarray:
    """
    Generate embeddings for a list of texts.

    Returns an ndarray of shape (len(texts), embedding_dim).
    """
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

def generate_embedding(text: str) -> list[float]:
    """Generate a single embedding vector."""
    model = _get_model()
    vec = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
    return vec[0].tolist()

async def store_entity_embeddings(
    session: AsyncSession,
    entities: Sequence[dict[str, Any]],
    batch_size: int = 64,
) -> int:
    """
    Generate and store embeddings for a batch of entities.

    Returns the count of embeddings stored.
    """
    if not entities:
        return 0

    count = 0
    for i in range(0, len(entities), batch_size):
        batch = entities[i : i + batch_size]
        texts = [entity_to_text(e) for e in batch]
        vectors = generate_embeddings(texts)

        for entity, vec in zip(batch, vectors):
            await session.execute(
                text("""
                    INSERT INTO entity_embeddings (entity_id, embedding)
                    VALUES (:id, :vec)
                    ON CONFLICT (entity_id) DO UPDATE SET embedding = EXCLUDED.embedding
                """),
                {"id": str(entity["id"]), "vec": vec.tolist()},
            )
            count += 1

    logger.info("entity_embeddings_stored", count=count)
    return count

async def store_claim_embeddings(
    session: AsyncSession,
    claims: Sequence[dict[str, Any]],
    batch_size: int = 64,
) -> int:
    """Generate and store embeddings for a batch of claims."""
    if not claims:
        return 0

    count = 0
    for i in range(0, len(claims), batch_size):
        batch = claims[i : i + batch_size]
        texts = [claim_to_text(c) for c in batch]
        vectors = generate_embeddings(texts)

        for claim, vec in zip(batch, vectors):
            await session.execute(
                text("""
                    INSERT INTO claim_embeddings (claim_id, embedding)
                    VALUES (:id, :vec)
                    ON CONFLICT (claim_id) DO UPDATE SET embedding = EXCLUDED.embedding
                """),
                {"id": str(claim["id"]), "vec": vec.tolist()},
            )
            count += 1

    logger.info("claim_embeddings_stored", count=count)
    return count

async def search_similar_entities(
    session: AsyncSession,
    query_text: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Find entities most similar to a query text using cosine similarity.

    Returns entities with their similarity scores.
    asyncpg does not support :param::type cast syntax, so we inline the
    vector literal directly into the SQL string.
    """
    query_vec = generate_embedding(query_text)
    vec_str = "[" + ",".join(str(float(x)) for x in query_vec) + "]"
    result = await session.execute(
        text(f"""
            SELECT e.*, 1 - (ee.embedding <=> '{vec_str}'::vector) AS similarity
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            ORDER BY ee.embedding <=> '{vec_str}'::vector
            LIMIT :limit
        """),
        {"limit": limit},
    )
    return [dict(row._mapping) for row in result.fetchall()]

async def search_similar_claims(
    session: AsyncSession,
    query_text: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find claims most similar to a query text."""
    query_vec = generate_embedding(query_text)
    vec_str = "[" + ",".join(str(float(x)) for x in query_vec) + "]"
    result = await session.execute(
        text(f"""
            SELECT c.*,
                   s.canonical_name AS subject_name,
                   o.canonical_name AS object_name,
                   1 - (ce.embedding <=> '{vec_str}'::vector) AS similarity
            FROM claim_embeddings ce
            JOIN claims c ON ce.claim_id = c.id
            JOIN entities s ON c.subject_id = s.id
            JOIN entities o ON c.object_id = o.id
            ORDER BY ce.embedding <=> '{vec_str}'::vector
            LIMIT :limit
        """),
        {"limit": limit},
    )
    return [dict(row._mapping) for row in result.fetchall()]
