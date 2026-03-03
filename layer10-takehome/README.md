# Layer10 Take-Home — Organizational Memory Graph

An email-derived organizational memory graph built on the Enron email corpus.
Extracts structured entities, claims, and evidence from raw emails using LLM
pipelines, stores them in PostgreSQL + pgvector, and serves a grounded
retrieval API with a React + D3.js visualization.

## Quick Start

```bash
# 1. Clone & install
cp .env.example .env          # Edit with your API keys
pip install -r requirements.txt

# 2. Start Postgres (pgvector)
make db

# 3. Download Enron dataset (~1.7GB)
make download

# 4. Run ingestion pipeline
make ingest

# 5. Run LLM extraction
make extract

# 6. Start API server
make serve
```

## Architecture

```
Email Corpus → Ingestion → LLM Extraction → Validation → Canonicalization → Graph Storage → Retrieval API → Visualization
```

## Key Design Decisions

- **PostgreSQL over Neo4j:** Simpler deployment, adjacency tables sufficient at this scale, pgvector enables hybrid search.
- **Evidence-first extraction:** Every claim must have a verifiable text excerpt — memory without grounding is hallucination.
- **Temporal validity windows:** Claims track `[valid_from, valid_to)` intervals, supporting "what was true" vs "what is true."
- **Reversible merges:** Entity resolution errors are undoable via an audit trail.
- **Hybrid retrieval:** Keyword + embedding + fuzzy matching for robust coverage.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/query?q=` | Natural language query → grounded context pack |
| `GET /api/entity/{id}` | Entity details + aliases |
| `GET /api/entity/{id}/claims` | All claims for an entity |
| `GET /api/claim/{id}/evidence` | Evidence supporting a claim |

## Project Structure

```
layer10-takehome/
├── config.py              # Centralized settings
├── logging_config.py      # Structured logging
├── ingestion/             # Email parsing, threading
├── extraction/            # LLM extraction pipeline
├── dedup/                 # Entity resolution, merge audit
├── storage/               # Database layer, schema, embeddings
├── retrieval/             # FastAPI app, graph traversal
├── visualization/         # React + D3.js frontend
├── tests/                 # Test suite
└── docs/                  # Write-up & documentation
```
