# Layer10 Memory Graph — Full Architecture & Technical Report

> **Date:** March 6, 2026  
> **Project:** Enron Email → Organizational Memory Graph  
> **Author:** Engineering Report (Auto-Generated from Source)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Component Deep Dive](#3-component-deep-dive)
   - 3.1 [Ingestion Layer](#31-ingestion-layer)
   - 3.2 [Signal Filter](#32-signal-filter)
   - 3.3 [Email Deduplication](#33-email-deduplication)
   - 3.4 [LLM Extraction Engine](#34-llm-extraction-engine)
   - 3.5 [Extraction Validator](#35-extraction-validator)
   - 3.6 [Entity Resolver & Canonicalization](#36-entity-resolver--canonicalization)
   - 3.7 [Storage Layer (PostgreSQL + pgvector)](#37-storage-layer-postgresql--pgvector)
   - 3.8 [Embedding Engine](#38-embedding-engine)
   - 3.9 [Retrieval API](#39-retrieval-api)
4. [Data Models & Schema](#4-data-models--schema)
5. [Mathematics & Algorithms](#5-mathematics--algorithms)
   - 5.1 [SHA-256 Exact Dedup Hashing](#51-sha-256-exact-dedup-hashing)
   - 5.2 [MinHash & Jaccard Similarity for Near-Dedup](#52-minhash--jaccard-similarity-for-near-dedup)
   - 5.3 [Name Similarity Scoring (Composite Weighted Formula)](#53-name-similarity-scoring-composite-weighted-formula)
   - 5.4 [Evidence Verification via Fuzzy Partial Matching](#54-evidence-verification-via-fuzzy-partial-matching)
   - 5.5 [Vector Embeddings & Cosine Similarity Search](#55-vector-embeddings--cosine-similarity-search)
   - 5.6 [IVFFlat Approximate Nearest Neighbour Index](#56-ivfflat-approximate-nearest-neighbour-index)
   - 5.7 [Confidence Scoring Model](#57-confidence-scoring-model)
   - 5.8 [Versioning Hash (Prompt + Schema + Model)](#58-versioning-hash-prompt--schema--model)
   - 5.9 [Graph Traversal & Diversity Cap](#59-graph-traversal--diversity-cap)
6. [Pipeline Orchestration & Idempotency](#6-pipeline-orchestration--idempotency)
7. [Temporal Model for Claims](#7-temporal-model-for-claims)
8. [Quality Control Strategy](#8-quality-control-strategy)
9. [Technology Stack](#9-technology-stack)
10. [Scalability & Production Path](#10-scalability--production-path)

---

## 1. System Overview

The Layer10 Memory Graph pipeline ingests raw corporate emails (Enron dataset, ~500K messages), converts them into a structured knowledge graph of **entities** (people, organizations, projects, topics, documents, meetings) and **claims** (typed, evidence-backed relationships between those entities), and exposes the graph through a retrieval API that can answer natural language questions with full provenance back to source emails.

The key design principle throughout is **evidence-first**: no claim exists in the system without a verbatim excerpt from the source email that supports it. This prevents the system from becoming a hallucination graph.

```
Raw Email Files (.maildir)
        │
        ▼
┌─────────────────┐
│  Ingestion      │  Parse → Filter → Dedup → Store raw emails
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  LLM Extraction │  Prompt → Groq/Gemini → Validate → Store entities/claims
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Canonicalization│ Entity resolution → Merge duplicate entities
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Embeddings     │  all-MiniLM-L6-v2 → pgvector store
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Retrieval API  │  FastAPI → Entity link → Graph expand → Context pack
└─────────────────┘
```

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Layer10 Pipeline                                   │
│                                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │  parse_  │   │ signal_  │   │  dedup_  │   │ thread_  │   │raw_email │  │
│  │  enron   │──▶│  filter  │──▶│  emails  │──▶│ builder  │──▶│ postgres │  │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘  │
│                                                                      │       │
│                                                                      ▼       │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │  prompts │   │ Extractor│   │validator │   │  entity  │   │  claims  │  │
│  │  (few-   │──▶│  (Groq/  │──▶│ (evidence│──▶│ resolver │──▶│ evidence │  │
│  │  shot)   │   │ Gemini)  │   │ grounding│   │          │   │ postgres │  │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘  │
│                                                                      │       │
│                                                                      ▼       │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │embedding │   │ pgvector │   │  entity  │   │  graph   │   │ FastAPI  │  │
│  │ all-MiniLM   │ (384-dim)│──▶│  linker  │──▶│ traversal│──▶│  /query  │  │
│  │  L6-v2)  │──▶│ IVFFlat  │   │(keyword+ │   │(BFS, n-  │   │  /entity │  │
│  └──────────┘   └──────────┘   │ embed+   │   │  hop)    │   │  /graph  │  │
│                                │ fuzzy)   │   └──────────┘   └──────────┘  │
│                                └──────────┘                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Infrastructure** (via `docker-compose.yml`):
- **PostgreSQL 16** with `pgvector`, `pg_trgm`, `uuid-ossp` extensions
- **Python 3.13** async runtime (`asyncio`, `asyncpg`)
- **Pydantic v2** for schema validation throughout
- **FastAPI** for the retrieval HTTP API
- **React + Vite** frontend (webui/)

---

## 3. Component Deep Dive

### 3.1 Ingestion Layer

**Module:** `ingestion/parse_enron.py`

Walks the Enron `.maildir` directory tree using `pathlib.Path`. For each email file:

1. Opens the file with Python's stdlib `email` module.
2. Extracts headers: `Message-ID`, `From`, `To/Cc/Bcc`, `Date`, `Subject`, `In-Reply-To`, `References`.
3. Extracts body — for multipart messages, walks MIME parts and returns the first `text/plain` part.
4. Produces a frozen `RawEmail` dataclass (immutable, `__slots__=True` for memory efficiency).

**Key computed properties on `RawEmail`:**

```python
body_hash  = SHA256(body.encode("utf-8"))
dedup_key  = SHA256(f"{sender}|{date_iso}|{subject}|{body_hash}")
```

The `dedup_key` is the primary artifact deduplication key — identical emails appearing in multiple user folders share the same key.

**Scope control:** A configurable `enron_user_list` limits parsing to a subset of users (default: 10 key users) to stay within LLM rate limits. Parsing is intentionally **lenient** — malformed headers are logged and skipped.

---

### 3.2 Signal Filter

**Module:** `ingestion/signal_filter.py`

A cheap pre-filter that runs **before LLM extraction** to eliminate ~70% of emails that are unlikely to yield organizational knowledge. Three criteria, all must pass:

| Filter | Condition |
|--------|-----------|
| **Folder filter** | Email folder must NOT be in `{all_documents, deleted_items, notes_inbox, contacts, calendar, straw}` |
| **Body length** | `len(body.strip()) >= 50` characters |
| **Keyword signal** | At least **2** signal keywords found in `subject + body` (case-insensitive) |

The keyword list (55 terms) covers: key Enron executives, named entities (LJM, Raptor, Azurix), financial/legal signals (`mark-to-market`, `10-k`, `restatement`), decision verbs (`approved`, `decided`, `resigned`), and risk terms (`write-down`, `collateral`).

**Reduction target:** ~70% of emails dropped before any LLM call, reducing cost significantly.

---

### 3.3 Email Deduplication

**Module:** `ingestion/dedup_emails.py`

Three-level artifact deduplication. Every decision is logged as an immutable `DedupEvent` for audit and reversibility.

#### Level 1 — Exact Dedup
Emails sharing the same `dedup_key` (SHA-256 of sender+date+subject+body_hash) are exact duplicates. The first-seen email becomes canonical; all others are recorded in `email_dedup_log` with `reason = "exact_duplicate"` and `similarity = 1.0`.

#### Level 2 — Quote Stripping
Before near-dedup, quoted/forwarded blocks are stripped using four regex patterns:
- `^>+\s?.*$` — inline quoted lines
- `---+ Original Message ---+.*` — Outlook-style forward headers
- `---+ Forwarded by .*---+.*` — Lotus Notes forward headers
- `On ... wrote:$` — Reply attribution lines

The **stripped body** is used for extraction; the **original `raw_text`** is preserved for evidence pointing.

#### Level 3 — Near-Dedup (MinHash LSH)
Near-duplicates (forwarded or slightly modified copies) are detected using **MinHash with Locality-Sensitive Hashing**:

```
threshold = 0.85 Jaccard similarity
num_perm  = 128 hash permutations
shingle k = 5 character k-shingles
```

The `datasketch` library maintains a MinHashLSH index. Emails with Jaccard similarity ≥ 0.85 are clustered; the earliest email is kept as canonical.

---

### 3.4 LLM Extraction Engine

**Modules:** `extraction/extractor.py`, `extraction/prompts.py`

The core intelligence component. Converts raw email text into structured entities and claims via an LLM.

#### LLM Client

- **Primary:** Groq API (`llama-3.3-70b-versatile`) — high throughput on free tier
- **Fallback:** Google Gemini (`gemini-2.0-flash`) — triggered automatically if primary exhausts retries
- **Provider switching** is configurable (`use_google_primary=True` reverses the roles)
- **Rate limiting:** `aiolimiter.AsyncLimiter(max_rate=80, time_period=60)` — stays within 80 RPM free-tier limit
- **Temperature:** `0.1` — near-deterministic output for consistent structured extraction

#### Prompt Design

The system prompt enforces:
- JSON-only output (no markdown fences)
- Explicit output schema with typed enums for entity/claim types
- Evidence-first rule: `evidence_excerpt` **must** be a verbatim substring of the email body
- Confidence scoring rubric (0.9–1.0 = explicit, 0.7–0.8 = strongly implied, 0.5–0.6 = weakly implied)
- Anti-hallucination guardrails ("Do NOT hallucinate entities not supported by the text")

**Few-shot examples:** Two hand-crafted Enron-style email → extraction pairs are prepended to every request to calibrate the model's behavior.

#### Retry Loop with Error Feedback

```
for attempt in 1..max_retries:
    response = await llm.call(messages)
    result   = validate(response)
    if result.is_valid:
        return result
    # Append error message to conversation for LLM self-correction
    messages.append({"role": "user", "content": f"Error: {last_error}. Fix the JSON."})

# Last resort: try fallback provider
response = await llm.call(messages, use_fallback=True)
```

This self-correction pattern significantly reduces parse failures on free-tier models.

#### Async Batch Processing

```python
semaphore = asyncio.Semaphore(concurrency=20)
results = await asyncio.gather(*[process(em) for em in emails])
```

Up to 20 concurrent LLM calls, bounded by both the semaphore and the rate limiter.

---

### 3.5 Extraction Validator

**Module:** `extraction/validator.py`

A four-stage validation pipeline that runs on every LLM response before storage:

```
Raw LLM JSON
     │
     ▼ Stage 1: JSON Parse
     │  • Strip markdown fences (```json...```)
     │  • Locate first { to last } as fallback
     │
     ▼ Stage 2: Pydantic Schema Validation
     │  • ExtractionResult.model_validate(data)
     │  • Entity types must be in EntityType enum
     │  • Claim types must be in ClaimType enum
     │  • Confidence must be in [0.0, 1.0]
     │  • Cross-reference: claim.subject and claim.object
     │    must match an extracted entity name/alias
     │
     ▼ Stage 3: Evidence Grounding (core quality gate)
     │  • For each claim.evidence_excerpt:
     │    1. Exact substring match in email body
     │    2. Normalized match (collapse whitespace)
     │    3. Fuzzy partial match (rapidfuzz.partial_ratio ≥ 90)
     │  • Claims failing all three → DROPPED
     │
     ▼ Stage 4: Confidence Threshold
        • Claims with confidence < extraction_min_confidence (0.4) → DROPPED
        • All drops logged as ValidationEvent for audit
```

The validator also repairs minor LLM transcription errors in excerpts (e.g., extra spaces) via fuzzy matching before dropping.

---

### 3.6 Entity Resolver & Canonicalization

**Module:** `dedup/entity_resolver.py`

Resolves multiple surface forms of the same real-world entity into a single canonical record. Uses a **greedy clustering** approach over three passes:

#### Pass 1 — Email Address Clustering (confidence: 0.95)
Entities sharing an email address in their aliases are merged. The entity with the **longest canonical name** becomes the representative (heuristic: longer name = more information).

#### Pass 2 — Fuzzy Name Matching (confidence: name_similarity score)
Within each entity type, all pairs of entities are compared using a composite similarity score (see §5.3). Pairs with score ≥ `name_threshold` (0.82 by default) are merged greedily from highest to lowest similarity. Merges are transitive.

#### Merge Operation

```python
canonical.absorb(other):
    canonical.aliases.add(other.canonical_name)
    canonical.aliases.update(other.aliases)
    canonical.email_addresses.update(other.email_addresses)
    # Properties: keep canonical's on conflict
    for k, v in other.properties.items():
        if k not in canonical.properties:
            canonical.properties[k] = v
```

Every merge produces a `MergeEvent` (audit record) stored in `merge_events` with `reason`, `confidence`, and reversibility support (`reversed_at` timestamp).

---

### 3.7 Storage Layer (PostgreSQL + pgvector)

**Module:** `storage/schema.sql`, `storage/db.py`

PostgreSQL 16 is the single store for all data. The schema has 10 tables:

| Table | Purpose |
|-------|---------|
| `raw_emails` | Parsed email storage, indexed by `dedup_key` |
| `email_threads` | Threaded conversation groups |
| `email_dedup_log` | Artifact-level dedup audit trail |
| `entities` | Graph nodes — canonical entities with JSONB properties |
| `claims` | Graph edges — typed relationships with temporal validity |
| `evidence` | Evidence pointers linking claims to source emails |
| `entity_embeddings` | 384-dim vectors for semantic search (pgvector) |
| `claim_embeddings` | 384-dim vectors for claim similarity search |
| `processing_log` | Idempotency log: `(email_id, extraction_version)` UNIQUE |
| `merge_events` | Entity/claim merge audit trail with reversal support |
| `extraction_configs` | Prompt/model/schema version registry |

**Key PostgreSQL extensions used:**
- `pgvector` — vector similarity search (ANN)
- `pg_trgm` — trigram fuzzy text search on entity names
- `uuid-ossp` — UUID primary keys

**Indexing strategy:**
```sql
-- Entity search paths
idx_entities_name_trgm  → GIN index for fuzzy pg_trgm search
idx_entities_aliases    → GIN index on TEXT[] aliases array
idx_entities_type       → B-tree for type filtering

-- Claim traversal
idx_claims_subject      → B-tree for forward traversal
idx_claims_object       → B-tree for reverse traversal
idx_claims_current      → Partial B-tree WHERE is_current = true

-- Vector search
idx_entity_embedding    → IVFFlat (lists=100), cosine distance
idx_claim_embedding     → IVFFlat (lists=100), cosine distance
```

**Upsert semantics:** All entity inserts use `ON CONFLICT DO NOTHING/UPDATE`. Processing log uses `UNIQUE(email_id, extraction_version)` to enforce idempotency at the database level.

---

### 3.8 Embedding Engine

**Module:** `storage/embeddings.py`

Generates dense vector representations of entities and claims for semantic similarity search.

**Model:** `sentence-transformers/all-MiniLM-L6-v2`
- Output dimension: **384**
- Training: Sentence-level semantic similarity (MS MARCO, NLI, STS tasks)
- Normalization: L2-normalized before storage (unit vectors → cosine similarity = dot product)

**Entity text representation** (rich context for better vectors):
```
"{canonical_name} ({entity_type}) also known as: {aliases} role: {role} title: {title}"
```

**Claim text representation:**
```
"{subject_name} {claim_type} {object_name} {key_properties}"
```

Including type and relationship context improves retrieval accuracy versus embedding the name alone.

---

### 3.9 Retrieval API

**Module:** `retrieval/api.py`, `retrieval/linker.py`, `retrieval/traversal.py`, `retrieval/context_pack.py`

A FastAPI application serving the memory graph. The main query endpoint (`GET /api/query?q=...`) runs a three-stage pipeline:

#### Stage 1: Entity Linking (`retrieval/linker.py`)

Maps the natural language question to seed entities using four strategies, merged and deduplicated:

1. **Regex NER:** Capitalized multi-word phrases (`[A-Z][a-z]+(\s[A-Z][a-z]+)+`) extracted as candidate names → exact/alias match against entities table
2. **Fuzzy trigram match:** PostgreSQL `pg_trgm` similarity on extracted names (≥ similarity threshold)
3. **Embedding similarity:** Encode the full question → cosine ANN search via pgvector (top-10 results)
4. **Fallback word search:** If no candidates found, split question into words >3 chars and run fuzzy match on each

#### Stage 2: Graph Expansion (`retrieval/traversal.py`)

BFS from seed entities up to `depth` hops (default 1, max 3):

```
for each hop:
    for each entity in frontier:
        claims = get_claims(entity, min_confidence, current_only)
        apply diversity cap (max N claims per claim_type)
        collect evidence for each claim
        add undiscovered neighbor entities to next frontier
```

**Diversity cap:** `max_claims_per_type = 5` prevents explosion on highly-connected nodes (e.g., "Enron Corp" would otherwise return thousands of `WORKS_AT` claims).

#### Stage 3: Context Pack Assembly (`retrieval/context_pack.py`)

Assembles the graph data into a structured `ContextPack` response that includes:
- Matched entities with summaries
- Claims with their evidence snippets
- Source attribution (message_id, excerpt, confidence)

**Available endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /api/query?q=...` | Natural language query → ContextPack |
| `GET /api/entity/{id}` | Entity details + alias list |
| `GET /api/entity/{id}/claims` | All claims for an entity with evidence |
| `GET /api/claim/{id}/evidence` | Evidence records for a specific claim |
| `GET /api/graph` | Full graph visualization data (nodes + edges) |
| `GET /api/stats` | Pipeline statistics |

---

## 4. Data Models & Schema

### Entity (Graph Node)

```
entities
├── id               UUID (PK)
├── canonical_name   TEXT           ← primary display name
├── entity_type      TEXT CHECK IN  ← Person|Organization|Project|Topic|Document|Meeting
├── aliases          TEXT[]         ← all known surface forms
├── properties       JSONB          ← type-specific attributes (role, title, etc.)
├── created_at       TIMESTAMPTZ
└── updated_at       TIMESTAMPTZ
```

### Claim (Graph Edge)

```
claims
├── id               UUID (PK)
├── claim_type       TEXT CHECK IN  ← WORKS_AT|REPORTS_TO|PARTICIPATES_IN|DISCUSSES|
│                                     DECIDED|MENTIONS|SENT_TO|REFERENCES_DOC|SCHEDULED
├── subject_id       UUID → entities(id)
├── object_id        UUID → entities(id)
├── properties       JSONB          ← e.g., {role: "VP", status: "confirmed"}
├── confidence       FLOAT [0,1]
├── valid_from       TIMESTAMPTZ    ← temporal validity window start
├── valid_to         TIMESTAMPTZ    ← NULL = currently valid
├── is_current       BOOLEAN
└── created_at       TIMESTAMPTZ
```

### Evidence (Claim ↔ Source)

```
evidence
├── id                   UUID (PK)
├── claim_id             UUID → claims(id)
├── source_type          TEXT           ← "email"
├── source_id            TEXT           ← message_id
├── excerpt              TEXT           ← verbatim quote from source
├── char_offset_start    INT            ← for precise highlighting
├── char_offset_end      INT
├── source_timestamp     TIMESTAMPTZ
├── extraction_version   TEXT           ← version tag of the extraction run
└── confidence           FLOAT [0,1]
```

### Processing Log (Idempotency)

```
processing_log
├── id                   UUID (PK)
├── email_id             TEXT
├── extraction_version   TEXT
├── status               TEXT CHECK IN ← pending|processing|completed|failed|superseded
├── raw_output           JSONB         ← full LLM response
├── validated_output     JSONB
├── error_message        TEXT
└── processed_at         TIMESTAMPTZ
│
UNIQUE(email_id, extraction_version)   ← enforces idempotency
```

---

## 5. Mathematics & Algorithms

### 5.1 SHA-256 Exact Dedup Hashing

**Used in:** `ingestion/parse_enron.py` — `body_hash` and `dedup_key`

```
body_hash = SHA-256( UTF-8(body) )                                  [64 hex chars]

dedup_key = SHA-256( UTF-8( f"{sender}|{date_iso}|{subject}|{body_hash}" ) )
```

SHA-256 produces a 256-bit (32-byte) digest. Collision probability for any two distinct emails is approximately:

```
P(collision) ≈ n² / (2 × 2²⁵⁶) ≈ 0   for n ≈ 500,000 emails
```

Exact dedup is O(n) — one hash computation and one set lookup per email.

---

### 5.2 MinHash & Jaccard Similarity for Near-Dedup

**Used in:** `ingestion/dedup_emails.py`

**Jaccard Similarity** between two email bodies A and B (as sets of character k-shingles):

```
J(A, B) = |A ∩ B| / |A ∪ B|   ∈ [0, 1]
```

Threshold: `J(A, B) ≥ 0.85` → near-duplicate.

**Problem:** Computing exact Jaccard over 500K pairs is O(n²) — infeasible.

**MinHash approximation:**

For each of `p = 128` independent hash permutations `hᵢ`:

```
MinHash signature:  sig(A)[i] = min_{s ∈ shingles(A)} hᵢ(s)
```

The key property is:

```
P( sig(A)[i] = sig(B)[i] ) = J(A, B)
```

So the fraction of matching signature positions is an **unbiased estimator** of Jaccard similarity:

```
Ĵ(A, B) = (1/p) × Σᵢ 𝟙[sig(A)[i] = sig(B)[i]]
```

**Estimation error:**

```
Var[Ĵ(A,B)] = J(1-J) / p
σ[Ĵ(A,B)] = √(J(1-J)/128) ≤ 1/(2√128) ≈ 0.044
```

With 128 permutations, the standard error is ≤ 4.4 percentage points.

**LSH Banding for sub-linear search:**

Divide the 128-hash signature into `b` bands of `r` rows each (`b × r = 128`). Two emails become a **candidate pair** (and are compared exactly) if they hash to the same bucket in **at least one band**:

```
P(candidate pair) = 1 - (1 - Jʳ)ᵇ
```

The LSH threshold (where the S-curve inflects) is approximately:

```
t ≈ (1/b)^(1/r)
```

With `threshold=0.85` and `num_perm=128`, `datasketch` selects `b` and `r` automatically to maximize precision at the threshold.

**Shingle generation:**

```python
shingles(text, k=5) = { text[i : i+k]  for i in range(len(text) - k + 1) }
```

Character 5-shingles are robust to word-level edits (insertions, deletions of individual words).

---

### 5.3 Name Similarity Scoring (Composite Weighted Formula)

**Used in:** `dedup/entity_resolver.py` — `name_similarity()`

A weighted composite of three signals:

```
sim(A, B) = 0.4 × full_ratio(A, B)
           + 0.3 × last_name_sim(A, B)
           + 0.3 × first_name_sim(A, B)
```

Where:

- **`full_ratio(A, B)`** = `rapidfuzz.fuzz.ratio(A.lower(), B.lower()) / 100`  
  This is the **normalized Levenshtein edit distance similarity**:
  ```
  full_ratio = 1 - (edit_distance(A, B) / max(|A|, |B|))
  ```

- **`last_name_sim(A, B)`**:
  ```
  last_name_sim = 1.0            if last_A == last_B  (exact match)
                = ratio(last_A, last_B)  otherwise
  ```

- **`first_name_sim(A, B)`**:
  ```
  first_name_sim = 0.9           if first_A.startswith(first_B)
                                  or first_B.startswith(first_A)  (nickname detection)
                = ratio(first_A, first_B)  otherwise
  ```

**Example — "Ken Lay" vs "Kenneth Lay":**
```
full_ratio    = 1 - (3 / max(7, 11)) = 1 - 0.27 = 0.73  →  weighted: 0.29
last_name_sim = 1.0  (exact "lay")                       →  weighted: 0.30
first_name_sim = 0.9  ("ken".startswith check)           →  weighted: 0.27
                                                         ──────────────────
Total                                                       0.86  ✓ (≥ 0.82)
```

**Merge threshold:** `name_threshold = 0.82`

**Greedy merge order:** Pairs are sorted by descending similarity and merged in order. Once entity A is merged into canonical C, A is removed from the entity map — subsequent pairs involving A are skipped.

---

### 5.4 Evidence Verification via Fuzzy Partial Matching

**Used in:** `extraction/validator.py` — `_find_excerpt_in_body()`

Three-tier check (short-circuits on first success):

```
1. Exact:     idx = body.find(excerpt)
              → accepted if idx ≠ -1

2. Normalized: norm_body    = collapse_whitespace(body)
               norm_excerpt = collapse_whitespace(excerpt)
               → accepted if norm_excerpt in norm_body

3. Fuzzy:     ratio = rapidfuzz.fuzz.partial_ratio(norm_excerpt, norm_body)
              → accepted if ratio ≥ 90
              → DROPPED otherwise
```

**`fuzz.partial_ratio`** finds the best-alignment window:

```
partial_ratio(A, B) = max_{substring S of B with |S|=|A|}  ratio(A, S)
```

The 90% threshold tolerates minor LLM transcription errors (e.g., collapsed whitespace, Unicode normalization differences) while rejecting fabricated excerpts.

---

### 5.5 Vector Embeddings & Cosine Similarity Search

**Used in:** `storage/embeddings.py`, `retrieval/linker.py`

**Model:** `sentence-transformers/all-MiniLM-L6-v2`
- Architecture: MiniLM (distilled BERT), 6 transformer layers
- Hidden size: 384
- Training: Multi-task contrastive learning on sentence pairs
- Output: L2-normalized vectors ∈ ℝ³⁸⁴ (unit sphere)

**Cosine similarity** between query embedding **q** and entity embedding **e**:

```
cos_sim(q, e) = (q · e) / (||q|| × ||e||)
```

Since vectors are L2-normalized at generation time (`normalize_embeddings=True`), `||q|| = ||e|| = 1`, so:

```
cos_sim(q, e) = q · e   (dot product suffices)
```

Values ∈ [-1, 1], where 1 = identical direction (semantically identical), 0 = orthogonal, -1 = opposite.

**pgvector query** (from `storage/embeddings.py`):

```sql
SELECT e.*, 1 - (emb.embedding <=> :query_vec) AS similarity
FROM entity_embeddings emb
JOIN entities e ON e.id = emb.entity_id
ORDER BY emb.embedding <=> :query_vec
LIMIT :limit
```

The `<=>` operator in pgvector computes **cosine distance** = `1 - cosine_similarity`. Ordering by ascending cosine distance = descending cosine similarity.

---

### 5.6 IVFFlat Approximate Nearest Neighbour Index

**Used in:** `storage/schema.sql`

```sql
CREATE INDEX idx_entity_embedding ON entity_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

**IVFFlat** (Inverted File with Flat quantization) is a two-phase ANN algorithm:

**Offline (index build):**
1. Run k-means clustering on all vectors to produce `lists = 100` centroids.
2. Assign each vector to its nearest centroid cluster.

**Online (query):**
1. Find the `probes` closest centroids to the query vector (default `probes=1`).
2. Perform exact cosine search within those clusters only.
3. Return top-k results.

**Complexity:**
```
Build:  O(n × lists × dim × iterations)  — k-means
Query:  O(probes × (n/lists) × dim)       — linear scan of probes clusters
                                             ≈ O(n × probes / lists)
```

With `lists=100` and `probes=1`, query scans ~1% of vectors → ~100× speedup over brute-force, at the cost of recall (typically 95–99% recall at these settings).

**Trade-off:** For production scale (millions of vectors), `ivfflat` would be replaced with HNSW (`CREATE INDEX ... USING hnsw`) or a dedicated vector store.

---

### 5.7 Confidence Scoring Model

**Used in:** `extraction/schema.py`, `extraction/prompts.py`

Confidence is a float in [0.0, 1.0] assigned by the LLM per claim, guided by the prompt's explicit rubric:

| Range | Meaning |
|-------|---------|
| 0.9 – 1.0 | Explicitly stated fact ("I am the VP of Trading") |
| 0.7 – 0.8 | Strongly implied ("As we discussed in our team meeting" → PARTICIPATES_IN) |
| 0.5 – 0.6 | Weakly implied, contextual inference |
| < 0.4 | Filtered out by the validator (below `extraction_min_confidence`) |

Confidence propagates through the system:
- Stored on `claims.confidence` and `evidence.confidence`
- Used as a filter in graph traversal (`min_confidence` query parameter, default 0.5)
- Used in entity linker ranking

Claims with the same `(subject_id, claim_type, object_id)` triple can have multiple evidence records — each with its own confidence from different extraction runs.

---

### 5.8 Versioning Hash (Prompt + Schema + Model)

**Used in:** `extraction/versioning.py`, `extraction/prompts.py`

The extraction version tag is a **deterministic fingerprint** of the extraction configuration:

```
version_tag = f"{schema_version}_{model_name}_{prompt_hash}"
```

Where:

```python
prompt_hash = SHA256(SYSTEM_PROMPT.encode("utf-8"))[:8]  (first 8 hex chars)
```

**Properties:**
- Same prompt + same model + same schema → same tag → idempotent (skips already-processed emails)
- Any change to prompt text → different `prompt_hash` → new tag → all emails reprocessed
- Any model upgrade → different `model_name` → new tag

The `processing_log` table enforces `UNIQUE(email_id, extraction_version)`. Pipeline uses a `LEFT JOIN` to find unprocessed emails:

```sql
SELECT r.* FROM raw_emails r
LEFT JOIN processing_log p
    ON r.message_id = p.email_id AND p.extraction_version = :version
WHERE p.id IS NULL
LIMIT :batch_size
```

This is **O(n)** with the right indexes and requires no coordination between pipeline runs.

---

### 5.9 Graph Traversal & Diversity Cap

**Used in:** `retrieval/traversal.py`

BFS from `k` seed entities with depth `d` and per-type diversity cap `c`:

```
nodes_visited ≤ k × (avg_neighbors per hop)^d × min(claims_per_type, c)
```

With `depth=1`, `max_claims_per_type=5`, 9 claim types:

```
max_claims_fetched = k × 9 × 5 = 45k  (per seed entity set)
```

The diversity cap prevents the "hub explosion" problem: a node like "Enron Corp" with thousands of `WORKS_AT` edges would overwhelm a context window if uncapped. The cap ensures diverse claim types are represented in the context.

**Confidence filter** applied before cap:

```
claims = [c for c in raw_claims if c["confidence"] >= min_confidence]
```

---

## 6. Pipeline Orchestration & Idempotency

**Module:** `pipeline.py`

The full pipeline is a three-stage async orchestrator:

```
run_full_pipeline()
    ├── Stage 1: run_ingestion()
    │     ├── iter_maildir() → parse all emails
    │     ├── filter_emails() → signal filter
    │     ├── deduplicate_emails() → exact + near dedup
    │     ├── build_threads() → thread grouping
    │     └── RawEmailRepository.upsert_batch() → DB store
    │
    ├── Stage 2: run_extraction()
    │     └── while unprocessed_emails:
    │           ├── RawEmailRepository.get_unprocessed(version_tag, limit=bs)
    │           ├── ProcessingLogRepository.mark_processing()
    │           ├── extractor.extract_batch(emails)
    │           └── store entities/claims/evidence
    │
    └── Stage 3: run_canonicalization()
          ├── EntityRepository.list_all()
          ├── resolve_entities() → EntityResolver.resolve()
          └── log_entity_merge() → persist merge events
```

**Resumability:** Each stage checks what has already been done:
- Ingestion: `ON CONFLICT DO NOTHING` on `dedup_key` — safe to re-run
- Extraction: `LEFT JOIN processing_log WHERE status IS NULL` — skips completed emails
- Canonicalization: `merge_events` log — idempotent if re-run on same data

**Error isolation:** Errors in one email do not stop the batch. Failed emails are recorded in `processing_log` with `status='failed'` and are automatically retried on the next run.

**Batch sizes:** Configurable via `Settings.extraction_batch_size` (default 10). Large batches amortize DB round-trip overhead; small batches reduce memory pressure and checkpoint frequency.

---

## 7. Temporal Model for Claims

The system maintains **three independent time dimensions** on every claim:

| Dimension | Column | Meaning |
|-----------|--------|---------|
| **Event time** | `valid_from` | When the real-world event occurred |
| **Validity time** | `valid_to` | When the claim stopped being true (NULL = still true) |
| **Extraction time** | `evidence.created_at` | When the claim was extracted |

This is a simplified **bitemporal model**. It enables queries like:
- "Who reported to Skilling as of January 2001?" (filter `valid_from <= 2001-01-01 AND (valid_to IS NULL OR valid_to > 2001-01-01)`)
- "What changed about Lay's role after August 2001?" (query multiple non-overlapping validity windows)

**Role change handling:**
```
Old claim:  WORKS_AT(Skilling, Enron) [valid_from=1997, valid_to=2001-08-14, is_current=false]
New claim:  WORKS_AT(Skilling, Enron) [valid_from=2001-08-14, valid_to=NULL, is_current=true]
```

The old claim is **never deleted** — it is soft-retired by setting `valid_to` and `is_current=false`. Both windows remain queryable.

---

## 8. Quality Control Strategy

### Evidence Verification Rate
**Definition:** Fraction of extracted claims where the evidence excerpt is found in the source email.

```
Evidence Verification Rate = (claims with verified excerpt) / (total extracted claims)
```

**Target: > 95%**. Claims failing verification are dropped before storage.

### Entity Resolution Precision
Manual audit of merge events:
```
Precision = (correct merges) / (total merges performed)
```
**Target: > 90%**. The `merge_events` table enables random sampling for audit.

### Confidence Calibration
Do claims at confidence 0.9 actually have ~90% accuracy?
```
Calibration Error = |mean(accuracy | conf ≈ 0.9) - 0.9|
```
Assessed by manually verifying a stratified sample of claims by confidence bucket.

### Automated Evaluation
The `tests/` directory contains:
- `test_extraction.py` — unit tests for the extractor/validator
- `test_dedup.py` — dedup correctness tests
- `test_ingestion.py` — ingestion pipeline tests

---

## 9. Technology Stack

| Category | Technology | Version / Notes |
|----------|-----------|-----------------|
| **Language** | Python | 3.13, async throughout |
| **Schema validation** | Pydantic v2 | Strict mode, field validators |
| **ORM / DB driver** | SQLAlchemy 2 (async) + asyncpg | Connection pooling, async sessions |
| **Database** | PostgreSQL 16 | pgvector, pg_trgm, uuid-ossp |
| **Vector search** | pgvector | IVFFlat index, cosine distance |
| **LLM (primary)** | Groq `llama-3.3-70b-versatile` | OpenAI-compatible API |
| **LLM (fallback)** | Google `gemini-2.0-flash` | OpenAI-compatible endpoint |
| **Embeddings** | `all-MiniLM-L6-v2` | sentence-transformers, 384-dim |
| **Fuzzy matching** | rapidfuzz | Levenshtein, partial ratio |
| **Near-dedup** | datasketch | MinHash LSH, Jaccard |
| **Rate limiting** | aiolimiter | Token bucket, async |
| **Retry logic** | tenacity | Exponential backoff |
| **API framework** | FastAPI | Async, Pydantic models |
| **Frontend** | React + Vite + TypeScript | webui/ |
| **Logging** | structlog (structured JSON) | `logging_config.py` |
| **Configuration** | pydantic-settings | `.env` + env vars |
| **Containerization** | Docker Compose | PostgreSQL + app |

---

## 10. Scalability & Production Path

| Aspect | Current (Prototype) | Production Target |
|--------|---------------------|-------------------|
| **Input scale** | ~500K emails, 10 users | Millions of artifacts (email + Slack + Jira + Docs + Calendar) |
| **Ingestion mode** | Batch file parsing | Event-driven (Kafka/SQS): new message → extract → store |
| **LLM** | Free-tier Groq / Gemini | Fine-tuned small model (extraction) + large model (disambiguation) |
| **Vector index** | IVFFlat (pgvector) | HNSW (higher recall) or dedicated vector store (Pinecone/Weaviate) |
| **Multi-tenancy** | Single workspace | Workspace-scoped queries, connection-level Row Level Security |
| **Identity resolution** | Email address + name | + Slack handles + Jira usernames + SSO identities |
| **Permissions** | Conceptual `source_access` table | Row-level security: filter claims by user's source access rights |
| **Deletions** | Soft-delete only | GDPR-aware: hard-delete evidence, cascade claim removal |
| **Observability** | Structured logs (stdout) | Datadog/Grafana pipeline: extraction latency, quality, cost dashboards |
| **Evaluation** | Manual audit | Automated regression: golden set with CI/CD F1 checks |
| **Entity types** | 6 types, 9 claim types | + Ticket, Sprint, Decision, Action Item, Channel |

**Key architectural changes for production:**
1. **Streaming ingestion:** Replace `iter_maildir` with an event consumer. Each new artifact triggers an individual extraction job, enabling near-real-time graph updates.
2. **HNSW indexing:** Replace IVFFlat with HNSW for better recall at high query throughput (IVFFlat recall degrades with `probes=1` at large scale).
3. **Extraction cost optimization:** Route simple extraction tasks to a fine-tuned small model; use large frontier models only for ambiguous disambiguation.
4. **Multi-tenant isolation:** Add `workspace_id` foreign keys and PostgreSQL RLS policies to isolate tenant data at the query level.

---

*Report generated from source: `/home/shriya/github_repos/layer10/layer10-takehome/`*
