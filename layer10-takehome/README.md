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

# 7. Export graph snapshot + example context packs
make export          # outputs/graph_export.json + outputs/graph_stats.json
make examples        # outputs/example_queries/*.json  (requires API running)

# 8. Start the visualization frontend
cd webui && npm install && npm run dev
```

## Architecture

```
Email Corpus → Ingestion → LLM Extraction → Validation → Canonicalization → Graph Storage → Retrieval API → Visualization
```

## Key Design Decisions

- **PostgreSQL over Neo4j:** Simpler deployment, adjacency tables sufficient at this scale, pgvector enables hybrid search.
- **Evidence-first extraction:** Every claim must have a verifiable text excerpt — memory without grounding is hallucination.
- **Temporal validity windows:** Claims track `[valid_from, valid_to)` on WORKS_AT/REPORTS_TO; the pipeline automatically closes the old claim's window when a conflicting one arrives.
- **Pending review queue:** Claims with confidence 0.4–0.5 are stored but flagged `pending_review=true`, surfaced via `GET /api/review-queue`.
- **Reversible merges:** Entity resolution errors are undoable via an audit trail (`merge_events` table); full history visible in the UI.
- **Hybrid retrieval:** Keyword + embedding + fuzzy matching for robust coverage.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/query?q=` | Natural language query → grounded context pack |
| `GET /api/query?q=&user_id=` | Same, with permissions filter (sources must be accessible to user_id) |
| `GET /api/entity/{id}` | Entity details + aliases |
| `GET /api/entity/{id}/claims` | All claims for an entity |
| `GET /api/entity/{id}/merges` | Full merge audit trail for an entity |
| `GET /api/claim/{id}/evidence` | Evidence supporting a claim |
| `GET /api/review-queue` | Claims flagged for human review (confidence 0.4–0.5) |
| `GET /api/graph` | Graph data for visualization |
| `GET /api/metrics` | Detailed observability metrics (quality, volume, temporal) |
| `GET /api/stats` | Summary counts |

Interactive docs: `http://localhost:8000/docs`

## Outputs

After running the pipeline:

```
outputs/
├── graph_export.json        # Full graph: nodes + edges + evidence map
├── graph_stats.json         # Summary statistics
└── example_queries/
    ├── index.json
    ├── q1_reporting.json            # "Who did Kenneth Lay report to?"
    ├── q2_california_energy.json    # "Key decisions about California energy trading?"
    ├── q3_skilling_risk.json        # "Who worked with Jeff Skilling on risk management?"
    └── q4_trading_strategy_history.json  # "What changed about Enron trading strategy over time?"
```

## Screenshots

See [docs/screenshots/README.md](docs/screenshots/README.md) for capture instructions.

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
