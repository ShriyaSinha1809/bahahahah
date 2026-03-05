-- =============================================================
-- Layer10 Memory Graph — PostgreSQL Schema
-- =============================================================
-- Requires: pgvector extension
-- Applied automatically via docker-compose init scripts.
--
-- Design rationale:
--   - Adjacency tables (entities + claims) model the graph.
--   - Every claim has evidence pointers back to source emails.
--   - Temporal validity windows on claims support "what was true."
--   - All merge/dedup actions logged for reversibility.
--   - pgvector embeddings enable hybrid semantic search.
-- =============================================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "vector";          -- pgvector (extension name is 'vector')
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- for fuzzy text search
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================
-- Raw Email Storage
-- =============================================================

CREATE TABLE IF NOT EXISTS raw_emails (
    message_id      TEXT PRIMARY KEY,
    sender          TEXT NOT NULL,
    recipients      TEXT[] NOT NULL DEFAULT '{}',
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    date            TIMESTAMPTZ,
    in_reply_to     TEXT,
    "references"    TEXT[] NOT NULL DEFAULT '{}',
    folder_path     TEXT NOT NULL DEFAULT '',
    raw_text        TEXT NOT NULL DEFAULT '',
    body_hash       TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raw_emails_sender ON raw_emails(sender);
CREATE INDEX IF NOT EXISTS idx_raw_emails_date ON raw_emails(date);
CREATE INDEX IF NOT EXISTS idx_raw_emails_dedup ON raw_emails(dedup_key);

-- =============================================================
-- Email Threads
-- =============================================================

CREATE TABLE IF NOT EXISTS email_threads (
    thread_id       TEXT PRIMARY KEY,
    subject         TEXT NOT NULL DEFAULT '',
    email_ids       TEXT[] NOT NULL DEFAULT '{}',
    participant_count INT NOT NULL DEFAULT 0,
    earliest        TIMESTAMPTZ,
    latest          TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================
-- Email Dedup Log (reversible audit trail)
-- =============================================================

CREATE TABLE IF NOT EXISTS email_dedup_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       TEXT NOT NULL,
    canonical_id    TEXT NOT NULL,
    reason          TEXT NOT NULL,       -- 'exact_duplicate', 'near_duplicate'
    similarity      FLOAT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================
-- Core Graph: Entities
-- =============================================================

CREATE TABLE IF NOT EXISTS entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL CHECK (entity_type IN (
        'Person', 'Organization', 'Project', 'Topic', 'Document', 'Meeting'
    )),
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    properties      JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entities_name_trgm ON entities
    USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entities_aliases ON entities USING gin(aliases);

-- =============================================================
-- Core Graph: Claims (edges / relationships)
-- =============================================================

CREATE TABLE IF NOT EXISTS claims (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_type      TEXT NOT NULL CHECK (claim_type IN (
        'WORKS_AT', 'REPORTS_TO', 'PARTICIPATES_IN', 'DISCUSSES',
        'DECIDED', 'MENTIONS', 'SENT_TO', 'REFERENCES_DOC', 'SCHEDULED'
    )),
    subject_id      UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    object_id       UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    properties      JSONB NOT NULL DEFAULT '{}',
    confidence      FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    valid_from      TIMESTAMPTZ,
    valid_to        TIMESTAMPTZ,        -- NULL = currently valid
    is_current      BOOLEAN NOT NULL DEFAULT true,
    pending_review  BOOLEAN NOT NULL DEFAULT false,  -- true = confidence 0.4–0.5, awaiting human review
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject_id);
CREATE INDEX IF NOT EXISTS idx_claims_object ON claims(object_id);
CREATE INDEX IF NOT EXISTS idx_claims_type ON claims(claim_type);
CREATE INDEX IF NOT EXISTS idx_claims_current ON claims(is_current) WHERE is_current = true;
CREATE INDEX IF NOT EXISTS idx_claims_confidence ON claims(confidence);
CREATE INDEX IF NOT EXISTS idx_claims_temporal ON claims(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_claims_pending_review ON claims(pending_review) WHERE pending_review = true;

-- =============================================================
-- Evidence Pointers
-- =============================================================

CREATE TABLE IF NOT EXISTS evidence (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id            UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL DEFAULT 'email',
    source_id           TEXT NOT NULL,       -- message_id
    excerpt             TEXT NOT NULL,
    char_offset_start   INT,
    char_offset_end     INT,
    source_timestamp    TIMESTAMPTZ,
    extraction_version  TEXT NOT NULL,
    confidence          FLOAT NOT NULL DEFAULT 0.5,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_id);

-- =============================================================
-- Embeddings (pgvector)
-- =============================================================

CREATE TABLE IF NOT EXISTS entity_embeddings (
    entity_id   UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    embedding   vector(384)     -- all-MiniLM-L6-v2
);

CREATE INDEX IF NOT EXISTS idx_entity_embedding ON entity_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS claim_embeddings (
    claim_id    UUID PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
    embedding   vector(384)
);

CREATE INDEX IF NOT EXISTS idx_claim_embedding ON claim_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =============================================================
-- Processing Log (idempotency + versioning)
-- =============================================================

CREATE TABLE IF NOT EXISTS processing_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id            TEXT NOT NULL,
    extraction_version  TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
        'pending', 'processing', 'completed', 'failed', 'superseded'
    )),
    raw_output          JSONB,
    validated_output    JSONB,
    error_message       TEXT,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(email_id, extraction_version)
);

CREATE INDEX IF NOT EXISTS idx_processing_log_email ON processing_log(email_id);
CREATE INDEX IF NOT EXISTS idx_processing_log_version ON processing_log(extraction_version);
CREATE INDEX IF NOT EXISTS idx_processing_log_status ON processing_log(status);

-- =============================================================
-- Extraction Configs (prompt/model/schema version tracking)
-- =============================================================

CREATE TABLE IF NOT EXISTS extraction_configs (
    version_tag     TEXT PRIMARY KEY,
    model_name      TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes           TEXT
);

-- =============================================================
-- Merge Events (entity + claim dedup audit trail)
-- =============================================================

CREATE TABLE IF NOT EXISTS merge_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_type     TEXT NOT NULL CHECK (action_type IN (
        'entity_merge', 'claim_merge', 'artifact_dedup'
    )),
    source_ids      TEXT[] NOT NULL,
    target_id       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    confidence      FLOAT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at     TIMESTAMPTZ,        -- non-null = undone
    reversed_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_merge_events_target ON merge_events(target_id);
CREATE INDEX IF NOT EXISTS idx_merge_events_type ON merge_events(action_type);
CREATE INDEX IF NOT EXISTS idx_merge_events_source_ids ON merge_events USING gin(source_ids);

-- =============================================================
-- Source Access (conceptual permissions model)
-- =============================================================

CREATE TABLE IF NOT EXISTS source_access (
    source_id       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    access_level    TEXT NOT NULL DEFAULT 'read',
    PRIMARY KEY (source_id, user_id)
);
