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

Claims with `valid_to = NULL` are "currently true." When information changes (e.g., role change), the old claim gets `valid_to` set and a new claim is created. Both remain queryable — nothing is overwritten.

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

| Aspect | Enron (Built) | Layer10 (Production) |
|---|---|---|
| **Sources** | Email only | Email + Slack + Jira + Docs + Calendar |
| **Ontology** | 6 entity types, 9 claim types | Add: Ticket, Sprint, Decision, Action Item, Channel |
| **Identity resolution** | Email address + name matching | + Slack handles + Jira usernames + SSO identity |
| **Durability** | All claims durable | Decisions/ownership = durable; casual chat = ephemeral |
| **Permissions** | Conceptual source_access table | Row-level security: filter claims by user's source access |
| **Deletions** | Soft-delete only | GDPR-aware: hard-delete evidence, cascade claim removal |
| **Scale** | ~500K emails, single-machine | Millions of artifacts; streaming ingestion (Kafka/SQS) |
| **Extraction cost** | Free-tier LLM | Fine-tuned small model for extraction, large model for disambiguation |
| **Evaluation** | Manual audit | Automated regression: golden set with CI/CD F1 checks |

### Key Architectural Changes for Production
1. **Streaming ingestion:** Replace batch file parsing with event-driven processing (new Slack message → extract → store)
2. **Embedding index scaling:** Move from ivfflat to HNSW indexes, or use a dedicated vector store (Pinecone/Weaviate)
3. **Multi-tenant isolation:** Workspace-scoped queries with connection-level RLS
4. **Observability:** Structured logging → Datadog/Grafana pipeline with extraction latency, quality, and cost dashboards
