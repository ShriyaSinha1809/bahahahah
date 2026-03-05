# Layer10 Take-Home — Technical Write-Up

## 1. Ontology & Schema Design

### Entity Types
We define six core entity types that cover the knowledge landscape of corporate email communication:

| Type | Rationale | Example |
|---|---|---|
| **Person** | Central nodes — most claims flow through people | Kenneth Lay, Jeff Skilling |
| **Organization** | Both internal (Enron Trading) and external (FERC) | Enron Corp, McKinsey |
| **Project** | Named initiatives referenced in emails | California Energy Deal |
| **Topic** | Abstract themes for topical clustering | Risk Management, Power Trading |
| **Document** | Referenced artifacts (reports, agreements) | Q3 Report |
| **Meeting** | Scheduled events mentioned in emails | Board Meeting Jan 14 |

### Claim Types (Edges)
Nine relationship types capture the key organizational dynamics:

- **WORKS_AT, REPORTS_TO** — Organizational structure
- **PARTICIPATES_IN** — Project involvement
- **DISCUSSES** — Topic tagging
- **DECIDED** — Decision tracking with status (proposed/confirmed/reversed)
- **MENTIONS, SENT_TO** — Communication patterns
- **REFERENCES_DOC, SCHEDULED** — Artifact and event linking

### Extensibility
Adding new entity/claim types requires:
1. Add to the `EntityType`/`ClaimType` enums in `extraction/schema.py`
2. Add to the SQL CHECK constraints in `storage/schema.sql`
3. Add few-shot examples to `extraction/prompts.py`

No structural changes needed — the JSONB properties column on entities and claims absorbs type-specific attributes.

---

## 2. Extraction Contract

### Prompt Design Philosophy
- **Evidence-first:** Every claim must cite a verbatim excerpt from the email. This is non-negotiable — it's the difference between a knowledge graph and a hallucination graph.
- **Few-shot grounding:** Two hand-crafted examples from Enron-style emails calibrate the model's behavior.
- **Conservative confidence:** The prompt explicitly defines the confidence scale and instructs the model not to hallucinate.

### Validation Pipeline
```
Raw LLM JSON → Parse → Schema Validate → Evidence Verify → Normalize → Filter
```

Each step is independently testable. The validator drops claims where the evidence excerpt cannot be found in the email body (exact or fuzzy match with >90% partial ratio). This is THE core quality control.

### Versioning
Every extraction run is tagged: `v{schema}_{model}_{prompt_hash}`. Same configuration → same tag → idempotent (skips already-processed emails). Changing any of {schema, model, prompt} generates a new tag, triggering re-extraction.

---

## 3. Dedup Strategy

### Three-Level Dedup

**Level 1 — Artifact (Email) Dedup:**
- Exact: SHA-256 of (sender, date, subject, body) catches copies across folders.
- Near: MinHash LSH (Jaccard > 0.85) catches forwarded/slightly-modified copies.
- Quote stripping: Separates new content from quoted blocks.

**Level 2 — Entity Canonicalization:**
- Email address clustering (highest confidence signal)
- Fuzzy name matching (Jaro-Winkler-inspired composite score)
- "Ken Lay" + "Kenneth Lay" + "ken.lay@enron.com" → single canonical entity

**Level 3 — Claim Dedup:**
- Same (subject_id, claim_type, object_id) → merge evidence pointers
- 50 emails saying "Ken is CEO" → 1 claim with 50 evidence records
- Temporal evolution preserved: different validity windows = different claims

### Reversibility
Every merge is logged in `merge_events` with source_ids, target_id, reason, confidence. Reversal sets `reversed_at` and restores the original state. This is critical for production trust — entity resolution is inherently error-prone.

---

## 4. Update Semantics

### Temporal Model
Three time dimensions:
1. **Event time:** When the real-world event happened (email sent, decision made)
2. **Validity time:** `[valid_from, valid_to)` on claims — when the claim was true
3. **Extraction time:** When we extracted the claim (for versioning)

Claims with `valid_to = NULL` are "currently true." When a conflicting claim for a mutually-exclusive relationship arrives (WORKS_AT, REPORTS_TO), the pipeline actively closes the old claim's validity window by setting `valid_to = new_claim.valid_from`. The old claim becomes historical (queryable with `include_historical=true`) and `is_current` flips to `false`. Both coexist in the graph — nothing is deleted.

### Idempotency
- `processing_log` tracks (email_id, extraction_version) with UNIQUE constraint
- Re-running with same version → skip (SELECT LEFT JOIN)
- Re-running with new version → process and mark old as superseded
- All inserts use ON CONFLICT DO NOTHING/UPDATE

### Backfill
When the schema or prompt changes:
1. Generate new version tag (hash changes)
2. Pipeline automatically processes all emails (old version entries don't match)
3. Old extractions remain in processing_log with previous version tags

---

## 5. Evaluation

### Quality Metrics
- **Evidence verification rate:** % of claims where the excerpt is found in the source. Target: >95%.
- **Entity resolution precision:** Manual audit of merge events. Target: >90% correct merges.
- **Confidence calibration:** Do claims at 0.9 confidence actually have ~90% accuracy?

### Human Review Queue
Claims extracted with confidence 0.4–0.5 are stored with `pending_review = true`. These are surfaced via `GET /api/review-queue` for human inspection before becoming "trusted" memory. A reviewer can confirm (update `pending_review = false`) or reject (delete). This is the human-in-the-loop quality gate that prevents borderline extractions from polluting durable memory.

### Observability Endpoint
`GET /api/metrics` returns a `MetricsResponse` covering:
- Volume: emails, entities, claims, evidence, merges
- Quality: `pending_review_claims`, `avg_confidence`, `low_confidence_claims`, `high_confidence_claims`
- Temporal: `current_claims` vs `historical_claims`
- Health: `failed_extractions`, `reversed_merges`

A spike in `pending_review_claims` or a drop in `avg_confidence` signals a prompt regression or model degradation.

### Audit Approach
1. Randomly sample 50 extraction results
2. Manually verify: Are entities correct? Are claims supported by evidence? Are any claims hallucinated?
3. Track precision (% of extracted claims that are correct) and recall (% of actual claims that were extracted)

### Known Limitations
- **Free-tier LLM limits:** ~5,000 emails processed (representative subset)
- **Single-source:** Email only — no Slack, Jira, docs
- **Name ambiguity:** "Ken" alone is ambiguous without email context
- **Temporal precision:** Many emails don't have explicit dates for the events they describe

---

## 6. Layer10 Adaptation

### Unstructured + Structured Fusion

Connecting email/chat discussions to structured work artifacts requires a unified identity layer and cross-source entity linking:

| Source type | Entity additions | Claim additions |
|---|---|---|
| Email | Person, Organization | SENT_TO, MENTIONS |
| Slack | Channel, Message | DISCUSSED_IN, REACTED_TO |
| Jira/Linear | Ticket, Sprint, Component | ASSIGNED_TO, TRANSITIONED, BLOCKS |
| Google Docs | Document, Section | AUTHORED, REVIEWED, REFERENCED |

A Slack thread referencing a Jira ticket ID becomes a `DISCUSSED_IN` claim linking the thread to the ticket entity. A `REFERENCES_DOC` claim from an email to a Google Doc creates a cross-source edge, letting the retrieval layer traverse from a question about a decision to the discussion that led to it.

### Long-Term Memory vs Ephemeral Context

Not all extracted content should become durable memory:

**Durable (graph-persisted):**
- Decisions and their reversals (`DECIDED` with `status=confirmed|reversed`)
- Ownership and role changes (`WORKS_AT`, `REPORTS_TO` with temporal windows)
- Document authorship and formal sign-offs
- Project participation and outcomes
- Anything cited 2+ times across independent sources (cross-evidence boosting)

**Ephemeral (expiry or lower retention tier):**
- Casual chat messages with no actionable content (Slack reactions, quick acknowledgements)
- Draft states of documents that were superseded
- Claims with single-source support and low confidence (<0.5) that haven't been confirmed by a reviewer

An `expires_at` column on claims (NULL = permanent) allows ephemeral context to be pruned on a schedule. Durable claims never expire unless explicitly redacted.

### Grounding & Safety: Deletions and Redactions

The evidence-first model gives precise deletion semantics:

1. **Source deletion:** When a source message is deleted (e.g., a Slack message removed by the sender), look up all `evidence` rows for that `source_id`. For each, check if the linked claim has *other* evidence. If yes, mark that evidence row soft-deleted. If the claim has *no remaining evidence*, soft-delete the claim too. Nothing is silently lost.

2. **GDPR redaction requests:** Hard-delete the `evidence` rows for the person's source messages. Cascade: claims with no remaining evidence are hard-deleted. Claims with other evidence survive but drop the redacted excerpts. This is audited in `merge_events` with `action_type='redaction'`.

3. **Provenance citations:** Every item returned by `GET /api/query` includes `EvidenceSnippet` records with `source_id`, `sender`, `excerpt`, and `source_date`. A consumer can always trace any memory item back to its exact source and verify it.

### Permissions

Memory retrieval must be constrained by the user's access to underlying sources:

**Model:** `source_access(source_id, user_id, access_level)` maps each source message to which users can see it. At query time, claims are filtered to only those with at least one evidence row in a source the requesting user can access.

**Implementation path:**
- `GET /api/query?user_id={uid}` passes the user context into the retrieval pipeline
- The graph expansion query joins `evidence` → `source_access` and only returns claims where a row exists for `(source_id, uid)`
- This is currently a post-retrieval filter (demonstrated) and in production would be pushed into the SQL as a subquery for efficiency: `EXISTS (SELECT 1 FROM source_access sa WHERE sa.source_id = e.source_id AND sa.user_id = :uid)`

**Implications:** A user who has access to email thread A but not Slack channel B will see claims grounded in A but not those grounded only in B. Claims with multi-source evidence show only the accessible excerpts.

### Operational Reality

| Concern | Current | Production path |
|---|---|---|
| **Ingestion scale** | Batch file parsing | Event-driven: Kafka/SQS consumer per source type |
| **Extraction throughput** | ~80 req/min (Groq free tier) | Fine-tune a small model (7B) on annotated samples; use large model only for ambiguous disambiguation |
| **Vector index** | IVFFlat (lists=100) | HNSW index (pgvector 0.7+ or Weaviate) for sub-10ms at 10M+ embeddings |
| **Multi-tenancy** | Single DB | Workspace-scoped connection pools + Postgres RLS policies |
| **Evaluation / regression** | Manual audit | Golden set of annotated email → extraction pairs; CI/CD computes F1 per entity/claim type; alert if F1 drops >5% |
| **Cost** | Free tier ($0) | ~$0.01–0.05 per email for extraction; amortize with incremental (only new emails) |
| **Observability** | structlog → stdout | Structured logs → Datadog; `GET /api/metrics` polled by Grafana; alert on `avg_confidence < 0.7` or `failed_extractions > 10%` |

