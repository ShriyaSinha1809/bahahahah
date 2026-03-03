# Layer10 Take-Home — Complete Implementation Plan

## Overview & Architecture

**Corpus:** Enron Email Dataset (~500K emails from ~150 users)
- Source: CMU CALO dataset (https://www.cs.cmu.edu/~enron/) or Kaggle mirror
- Why: Rich identity-resolution challenges, email threading/quoting, real organizational communication, well-studied

**LLM:** Groq free tier (Llama 3.3 70B) — 14,400 requests/day, 6,000 tokens/min
- Fallback: Google AI Studio (Gemini 2.5 Flash) — 1M tokens/min free tier
- Both offer OpenAI-compatible APIs, easy to swap

**Stack:**
- Python 3.11+ (extraction pipeline, backend API)
- PostgreSQL + pgvector (graph-like store with adjacency + vector search)
- FastAPI (retrieval API)
- React + D3.js force-directed graph (visualization)
- Pydantic (schema validation)

**Why Postgres over Neo4j?** Simpler to deploy, adjacency tables are sufficient for this scale, pgvector gives hybrid search, and it demonstrates real engineering judgment (not just "use a graph DB for graphs").

---

## Phase 1: Data Ingestion & Preprocessing (Days 1–2)

### 1.1 Download & Parse Enron Dataset

```
enron_raw/
  maildir/
    allen-p/
      inbox/
      sent/
      ...
    bass-e/
    ...
```

**Tasks:**
- Download the CMU tarball (~1.7GB compressed)
- Parse each email file extracting: `message_id`, `date`, `from`, `to`, `cc`, `bcc`, `subject`, `body`, `in_reply_to`, `references`, `x_folder`
- Use Python `email` stdlib for parsing headers
- Store raw emails in a `raw_emails` table

**Key code module:** `ingestion/parse_enron.py`

```python
# Pseudocode
@dataclass
class RawEmail:
    message_id: str
    date: datetime | None
    sender: str
    recipients: list[str]  # to + cc + bcc merged
    subject: str
    body: str
    in_reply_to: str | None
    references: list[str]
    folder_path: str  # e.g. "allen-p/inbox"
    raw_text: str  # full original for evidence pointers
```

### 1.2 Artifact Deduplication (Email Level)

- **Exact dedup:** Hash `(sender, date, subject, body_hash)` → skip true duplicates
- **Quote stripping:** Detect forwarded/quoted blocks (`> `, `-----Original Message-----`, `--- Forwarded by ---`) and extract the "new" content vs quoted content
- **Near-dedup:** MinHash/SimHash on body text → flag clusters with Jaccard > 0.85 → keep canonical (earliest), link others as aliases
- Store dedup decisions in an `email_dedup_log` table (reversible — stores merge reason, similarity score, canonical_id)

### 1.3 Thread Reconstruction

- Build threads using `in_reply_to` and `references` headers
- Fallback: subject-line matching (strip `Re:`, `Fwd:`) + time window + participant overlap
- Store in `email_threads` table with `thread_id` → `email_ids[]`

**Deliverable:** ~500K emails parsed, ~300-400K after dedup, organized into threads.

---

## Phase 2: Schema Design & Ontology (Day 2)

### 2.1 Entity Types

| Entity Type | Properties | Example |
|---|---|---|
| `Person` | canonical_name, email_aliases[], department, title, status | "Kenneth Lay" |
| `Organization` | name, aliases[], type (internal_dept / external_company) | "Enron Trading", "FERC" |
| `Project` | name, aliases[], status, time_range | "California Energy Deal" |
| `Topic` | name, description | "Power Trading", "Risk Management" |
| `Document` | name, type, referenced_in[] | "Q3 Report", "Draft Agreement" |
| `Meeting` | date, participants[], subject | meeting references in emails |

### 2.2 Claim / Relationship Types

| Claim Type | Subject → Object | Properties |
|---|---|---|
| `WORKS_AT` | Person → Organization | role, valid_from, valid_to |
| `REPORTS_TO` | Person → Person | valid_from, valid_to |
| `PARTICIPATES_IN` | Person → Project | role |
| `DISCUSSES` | Email → Topic | |
| `DECIDED` | Person/Group → Decision | date, status (proposed/confirmed/reversed) |
| `MENTIONS` | Email → Entity (any) | |
| `SENT_TO` | Person → Person | via email, count, time_range |
| `REFERENCES_DOC` | Email → Document | |
| `SCHEDULED` | Person → Meeting | |

### 2.3 Evidence Model

Every claim MUST have at least one evidence pointer:

```python
@dataclass
class Evidence:
    evidence_id: str
    source_type: str          # "email"
    source_id: str            # message_id
    excerpt: str              # exact text span supporting claim
    char_offset_start: int
    char_offset_end: int
    timestamp: datetime
    extraction_version: str   # "v1.0_llama3.3_schema_v2"
    confidence: float         # 0.0–1.0
```

### 2.4 Temporal Model

- **Event time:** when the thing actually happened (email sent, decision made)
- **Validity time:** `[valid_from, valid_to)` interval on claims — `valid_to = NULL` means "currently true"
- **Extraction time:** when we extracted this claim (for versioning)

Conflicts are stored, not overwritten: if Person X's role changes, the old claim gets `valid_to` set, new claim created. Both remain queryable.

---

## Phase 3: Structured Extraction Pipeline (Days 3–5)

### 3.1 Extraction Architecture

```
[Raw Email] → [Preprocessor] → [Chunker] → [LLM Extractor] → [Validator] → [Normalizer] → [Store]
```

**Scope down for the take-home:** Process a representative subset (~5,000–10,000 emails) from 10–15 high-activity users. This is enough to demonstrate the full pipeline without burning through free-tier limits.

### 3.2 Preprocessing & Chunking

- Strip email headers from body, but keep metadata in context
- For long emails, chunk at paragraph boundaries, keeping ~1500 tokens per chunk
- Provide thread context: include subject + previous email summary in the prompt window

### 3.3 LLM Extraction Prompts

**Strategy:** Structured JSON output with Pydantic validation. Use few-shot examples in system prompt.

```python
SYSTEM_PROMPT = """
You are an information extraction system. Given an email, extract:
1. Entities mentioned (people, organizations, projects, topics, documents)
2. Claims/relationships between entities
3. For each claim, quote the exact text excerpt that supports it

Output ONLY valid JSON matching this schema:
{
  "entities": [
    {"name": "...", "type": "Person|Organization|Project|Topic|Document",
     "aliases": ["..."], "properties": {"role": "...", ...}}
  ],
  "claims": [
    {"type": "WORKS_AT|REPORTS_TO|DECIDED|...",
     "subject": "entity_name",
     "object": "entity_name",
     "properties": {"role": "...", "status": "..."},
     "evidence_excerpt": "exact quote from email",
     "confidence": 0.0-1.0}
  ]
}

RULES:
- Only extract facts explicitly stated or strongly implied in the email
- evidence_excerpt must be a verbatim substring of the email
- confidence: 0.9+ for explicit statements, 0.6-0.8 for inferences
- Do NOT hallucinate entities or relationships
"""
```

**Few-shot examples:** Include 3–4 hand-annotated email → JSON pairs.

### 3.4 Validation & Repair

```python
def validate_extraction(raw_json: str, email_body: str) -> ExtractionResult:
    # 1. Parse JSON (retry up to 3x with error feedback if invalid)
    # 2. Validate against Pydantic schema
    # 3. Verify evidence_excerpt exists as substring in email_body
    #    - If not found: fuzzy match (Levenshtein < 10%) → repair offset
    #    - If no match: drop the claim, log warning
    # 4. Normalize entity names (strip whitespace, title-case people)
    # 5. Reject claims with confidence < 0.4
    # 6. Log all dropped/repaired claims for audit
```

### 3.5 Versioning & Backfill Strategy

- Every extraction run is tagged: `{model}_{schema_version}_{prompt_hash}_{timestamp}`
- Extractions table stores: `extraction_id, email_id, version_tag, raw_output, validated_output, status`
- **Backfill:** When schema changes, re-extract affected emails. Old extractions are soft-deleted (marked `superseded_by`), not hard-deleted.
- Track prompt/model/schema versions in a `extraction_configs` table

### 3.6 Quality Gates

1. **Confidence threshold:** Claims < 0.5 go to a "pending_review" queue
2. **Cross-evidence support:** Claims mentioned in 2+ emails get boosted confidence
3. **Contradiction detection:** If two claims conflict (e.g., Person X works at A and B simultaneously), flag for review
4. **Sampling audit:** Randomly sample 5% of extractions for manual review; track precision/recall over time

### 3.7 Rate Limit Management

```python
# Groq: 14,400 req/day, 6,000 tokens/min
# Strategy: async with rate limiter
import asyncio
from aiolimiter import AsyncLimiter

rate_limiter = AsyncLimiter(max_rate=80, time_period=60)  # stay under 6K tok/min

async def extract_email(email: RawEmail):
    async with rate_limiter:
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[...],
            response_format={"type": "json_object"}
        )
    return validate_extraction(response, email.body)
```

---

## Phase 4: Deduplication & Canonicalization (Days 5–6)

### 4.1 Entity Canonicalization

**People resolution is the hardest part.** Enron emails have many variations:
- "Kenneth Lay", "Ken Lay", "ken.lay@enron.com", "Lay, Kenneth", "Ken"

**Multi-signal approach:**
1. **Email address clustering:** All emails from same address → same person
2. **Name normalization:** Parse into (first, last) tuples, match fuzzy (Jaro-Winkler > 0.88)
3. **Context clues:** If "Ken" always appears in threads with ken.lay@enron.com → merge
4. **LLM-assisted disambiguation:** For ambiguous cases, send context to LLM: "Is 'Ken' in this email referring to Ken Lay or Ken Rice?"

```python
# Canonical entity table
class CanonicalEntity:
    canonical_id: str
    canonical_name: str
    entity_type: str
    aliases: list[str]       # all known surface forms
    merge_history: list[MergeEvent]  # reversible audit trail

class MergeEvent:
    merged_from: str         # entity_id that was absorbed
    merged_into: str         # canonical_id
    reason: str              # "email_address_match", "fuzzy_name_0.92", "llm_disambiguation"
    confidence: float
    timestamp: datetime
    reversed: bool           # for undo support
```

### 4.2 Claim Deduplication

- If 50 emails all say "Ken Lay is CEO of Enron" → one canonical claim with 50 evidence pointers
- **Merge criteria:** Same (subject_canonical_id, claim_type, object_canonical_id) within time window
- Keep all evidence pointers; the claim's confidence = max of individual confidences
- **Claim versioning:** If email from Jan says "Ken is CEO" and email from Dec says "Ken resigned," these are NOT duplicates — they're a temporal evolution → different validity windows

### 4.3 Conflict & Revision Handling

```
Claim: Ken Lay WORKS_AT Enron [role=CEO, valid_from=1986, valid_to=2001-01]
Claim: Ken Lay WORKS_AT Enron [role=Chairman, valid_from=2001-02, valid_to=2002-01]
```

- Conflicts detected by: overlapping validity windows for mutually exclusive claims
- Resolution: latest email timestamp wins for "current" status, but BOTH are stored
- UI shows timeline of claim evolution

### 4.4 Reversibility

Every merge/dedup action is stored as an event:
```sql
CREATE TABLE merge_events (
    id SERIAL PRIMARY KEY,
    action_type VARCHAR(20),    -- 'entity_merge', 'claim_merge', 'artifact_dedup'
    source_ids TEXT[],
    target_id TEXT,
    reason TEXT,
    confidence FLOAT,
    created_at TIMESTAMP,
    reversed_at TIMESTAMP NULL,  -- non-null = undone
    reversed_reason TEXT
);
```

Undo = restore original entities/claims and re-link evidence.

---

## Phase 5: Memory Graph Storage (Days 6–7)

### 5.1 PostgreSQL Schema

```sql
-- Core entities
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- Person, Organization, Project, Topic, Document
    aliases TEXT[],
    properties JSONB,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- Claims/relationships (edges)
CREATE TABLE claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_type TEXT NOT NULL,
    subject_id UUID REFERENCES entities(id),
    object_id UUID REFERENCES entities(id),
    properties JSONB,           -- role, status, etc.
    confidence FLOAT NOT NULL,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,         -- NULL = currently valid
    is_current BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT now()
);

-- Evidence pointers
CREATE TABLE evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id UUID REFERENCES claims(id),
    source_type TEXT DEFAULT 'email',
    source_id TEXT NOT NULL,     -- message_id
    excerpt TEXT NOT NULL,
    char_offset_start INT,
    char_offset_end INT,
    source_timestamp TIMESTAMP,
    extraction_version TEXT
);

-- Embeddings for hybrid search
CREATE TABLE entity_embeddings (
    entity_id UUID REFERENCES entities(id),
    embedding vector(384),       -- all-MiniLM-L6-v2
    PRIMARY KEY (entity_id)
);

CREATE TABLE claim_embeddings (
    claim_id UUID REFERENCES claims(id),
    embedding vector(384),
    PRIMARY KEY (claim_id)
);

-- Indexes
CREATE INDEX idx_claims_subject ON claims(subject_id);
CREATE INDEX idx_claims_object ON claims(object_id);
CREATE INDEX idx_claims_type ON claims(claim_type);
CREATE INDEX idx_claims_current ON claims(is_current) WHERE is_current = true;
CREATE INDEX idx_evidence_source ON evidence(source_id);
CREATE INDEX idx_entity_embedding ON entity_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 5.2 Incremental Ingestion & Idempotency

- Each email processed has an entry in `processing_log(email_id, extraction_version, status, processed_at)`
- Re-processing same email with same version = skip (idempotent)
- Re-processing with new version = create new extractions, mark old as superseded
- Edits/deletes: if a source email is deleted, soft-delete all claims that ONLY have evidence from that email

### 5.3 Permissions Model (Conceptual)

```sql
-- Every evidence row links to a source
-- Sources have access control
CREATE TABLE source_access (
    source_id TEXT,
    user_id TEXT,
    access_level TEXT  -- 'read', 'admin'
);

-- At query time: filter claims to only those with at least one
-- evidence row whose source_id the user can access
```

### 5.4 Observability

- Log: extraction latency, LLM error rate, validation failure rate, dedup merge rate
- Metrics dashboard: entities/day, claims/day, avg confidence, conflict rate
- Alert if: validation failure rate > 20%, confidence trending down, extraction latency spikes

---

## Phase 6: Retrieval & Grounding API (Days 7–8)

### 6.1 Retrieval Architecture

```
[Question] → [Entity Linker] → [Graph Traversal] → [Evidence Collector] → [Ranked Context Pack]
```

### 6.2 Question → Candidate Entities

**Hybrid approach:**
1. **Keyword extraction:** Pull named entities from question using spaCy NER
2. **Embedding search:** Encode question → search entity_embeddings with pgvector cosine similarity (top 10)
3. **Fuzzy name match:** trigram similarity on entity aliases
4. **Merge results:** union of all candidates, deduplicate

### 6.3 Graph Expansion

From candidate entities, expand:
- 1-hop: all claims where entity is subject or object
- Filter: `is_current = true` (unless question asks about history)
- Filter: `confidence >= 0.5`
- Diversity: cap at 5 claims per claim_type to avoid explosion
- Recency bias: weight recent claims higher

### 6.4 Context Pack Assembly

```python
@dataclass
class ContextPack:
    question: str
    entities: list[EntitySummary]
    claims: list[ClaimWithEvidence]
    conflicts: list[ConflictPair]  # show both sides
    total_evidence_count: int

@dataclass
class ClaimWithEvidence:
    claim_type: str
    subject: str
    object: str
    properties: dict
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None
    evidence: list[EvidenceSnippet]

@dataclass
class EvidenceSnippet:
    source_id: str
    excerpt: str
    source_date: datetime
    sender: str
    subject: str
```

### 6.5 Conflict Handling

When two claims conflict (e.g., different roles in overlapping time):
- Return BOTH with their evidence
- Label: "conflicting_claims" with timestamps
- Let the consumer decide (or show both in UI)

### 6.6 FastAPI Endpoints

```python
@app.get("/api/query")
async def query(q: str, include_historical: bool = False) -> ContextPack:
    ...

@app.get("/api/entity/{entity_id}")
async def get_entity(entity_id: str) -> EntityDetail:
    ...

@app.get("/api/entity/{entity_id}/claims")
async def get_claims(entity_id: str, claim_type: str = None) -> list[ClaimWithEvidence]:
    ...

@app.get("/api/claim/{claim_id}/evidence")
async def get_evidence(claim_id: str) -> list[EvidenceSnippet]:
    ...
```

**Example queries to demonstrate:**
1. "Who did Kenneth Lay report to?" → entities, claims, evidence
2. "What were the key decisions about California energy trading?" → topic-based retrieval
3. "Who worked with Jeff Skilling on risk management?" → multi-hop traversal
4. "What changed about Enron's trading strategy over time?" → temporal evolution

---

## Phase 7: Visualization Layer (Days 8–9)

### 7.1 Tech Stack

- **React** frontend (Vite for bundling)
- **D3.js force-directed graph** for entity/claim visualization
- **Side panel** for evidence drill-down
- **Timeline slider** for temporal filtering

### 7.2 Core Views

**Graph View:**
- Nodes = entities (colored by type: Person=blue, Org=green, Project=orange, etc.)
- Edges = claims (labeled by type, thickness = confidence)
- Click node → side panel shows entity details + all claims
- Click edge → side panel shows evidence excerpts with source metadata
- Filter controls: entity type, claim type, confidence threshold, time range
- Search bar: find entity by name

**Evidence Panel:**
- Shows exact excerpt highlighted in context
- Source metadata: email sender, date, subject, message_id
- Link to view full original email
- Shows extraction version and confidence

**Dedup/Merge Inspector:**
- Select an entity → see all aliases and merge history
- "Undo merge" button (calls API)
- See which entities were merged and why

**Timeline View:**
- Horizontal timeline showing claims about a selected entity
- Color-coded by claim type
- Shows when claims became active/invalid

### 7.3 Data Flow

```
React App → FastAPI → PostgreSQL
              ↓
         /api/graph?center_entity=X&depth=2&min_confidence=0.5
              ↓
         Returns: { nodes: [...], edges: [...], evidence_map: {...} }
```

---

## Phase 8: Write-Up & Layer10 Adaptation (Day 9–10)

### 8.1 Write-Up Structure

1. **Ontology & Schema** — rationale for entity/claim types, extensibility
2. **Extraction Contract** — prompt design, validation, versioning
3. **Dedup Strategy** — multi-level dedup, canonicalization approach, reversibility
4. **Update Semantics** — temporal model, idempotency, backfill
5. **Evaluation** — manual quality audit results, precision/recall estimates

### 8.2 Layer10 Adaptation

| Aspect | Enron (built) | Layer10 (adapted) |
|---|---|---|
| **Sources** | Email only | Email + Slack + Jira/Linear + Docs |
| **Ontology additions** | — | `Ticket`, `Sprint`, `Decision`, `Action Item`, `Channel` |
| **Unstructured + structured fusion** | Email threads | Slack thread → linked Jira ticket → referenced Google Doc |
| **Entity resolution** | Email aliases | Slack handles + Jira usernames + email → unified identity |
| **Durable vs ephemeral** | All claims durable | Decisions/ownership = durable; casual chat = ephemeral context |
| **Permissions** | Conceptual | Row-level security: claims filtered by source access per user |
| **Deletions/redactions** | Soft-delete cascade | GDPR-aware: hard-delete evidence, cascade claim removal if no other evidence |
| **Scale** | ~500K emails | Millions of artifacts; need streaming ingestion (Kafka/SQS), batch extraction, caching |
| **Cost** | Free tier LLM | Production: fine-tuned smaller model for extraction, large model for complex disambiguation |
| **Evaluation** | Manual audit | Automated regression: golden set of annotated artifacts, CI/CD runs extraction + checks F1 |

---

## File Structure

```
layer10-takehome/
├── README.md                    # Setup + run instructions
├── docs/
│   └── writeup.md              # Full write-up
├── ingestion/
│   ├── parse_enron.py          # Email parser
│   ├── dedup_emails.py         # Artifact deduplication
│   └── thread_builder.py       # Thread reconstruction
├── extraction/
│   ├── schema.py               # Pydantic models (Entity, Claim, Evidence)
│   ├── prompts.py              # LLM prompt templates
│   ├── extractor.py            # LLM extraction + rate limiting
│   ├── validator.py            # JSON validation + evidence verification
│   └── versioning.py           # Extraction version management
├── dedup/
│   ├── entity_resolver.py      # Person/org canonicalization
│   ├── claim_dedup.py          # Claim merging
│   └── merge_audit.py          # Reversible merge log
├── storage/
│   ├── schema.sql              # PostgreSQL DDL
│   ├── migrations/             # Schema migrations
│   ├── db.py                   # Database access layer
│   └── embeddings.py           # Embedding generation + storage
├── retrieval/
│   ├── api.py                  # FastAPI app
│   ├── linker.py               # Question → entity linking
│   ├── traversal.py            # Graph expansion
│   └── context_pack.py         # Context assembly
├── visualization/
│   ├── frontend/               # React + D3 app
│   │   ├── src/
│   │   │   ├── GraphView.tsx
│   │   │   ├── EvidencePanel.tsx
│   │   │   ├── Timeline.tsx
│   │   │   └── MergeInspector.tsx
│   │   └── package.json
│   └── screenshots/            # For submission
├── outputs/
│   ├── graph_export.json       # Serialized graph
│   └── example_queries/        # Context packs for sample questions
├── docker-compose.yml          # Postgres + API + Frontend
├── requirements.txt
└── Makefile                    # make download, make extract, make build, make serve
```

---

## Timeline Summary

| Phase | Days | Key Deliverable |
|---|---|---|
| 1. Data Ingestion | 1–2 | Parsed, deduped emails in DB |
| 2. Schema Design | 2 | Ontology defined, Pydantic models |
| 3. Extraction Pipeline | 3–5 | LLM extraction with validation, ~5-10K emails processed |
| 4. Deduplication | 5–6 | Canonical entities, merged claims, audit trail |
| 5. Graph Storage | 6–7 | Populated Postgres with embeddings |
| 6. Retrieval API | 7–8 | FastAPI with context pack responses |
| 7. Visualization | 8–9 | React + D3 graph explorer |
| 8. Write-Up & Polish | 9–10 | Documentation, Layer10 adaptation, screenshots |

---

## Key Design Decisions & Trade-offs

1. **Postgres over Neo4j:** Simpler deployment, adjacency tables are fine at this scale, pgvector gives hybrid search. Trade-off: less elegant graph traversal queries.

2. **Subset extraction (5-10K emails):** Free-tier LLM limits make full-corpus extraction impractical. Solution: pick high-activity users, demonstrate pipeline works end-to-end, explain how it scales.

3. **Groq + Llama 3.3 70B:** Best free-tier combo of intelligence + speed. 14,400 requests/day is enough for ~5K emails with retries. Fallback to Gemini Flash if needed.

4. **Evidence-first extraction:** Every claim must have a verifiable excerpt. This is THE differentiator for Layer10 — memory without grounding is hallucination.

5. **Temporal model with validity windows:** More complex than a simple snapshot, but essential for "long-term memory" — you need to know what WAS true vs what IS true.

6. **Reversible merges:** Entity resolution is inherently error-prone. Making merges undoable with an audit trail is critical for production trust.

7. **Hybrid retrieval (keyword + embedding + fuzzy):** No single retrieval method covers all cases. Combining them with union + reranking gives robust coverage.
