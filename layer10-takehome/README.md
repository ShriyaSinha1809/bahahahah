# Layer10 Take-Home — Organizational Memory Graph

An email-derived organizational memory graph built on the Enron email corpus.
Extracts structured entities, claims, and evidence from raw emails using LLM
pipelines, stores them in PostgreSQL + pgvector, and serves a grounded
retrieval API with a React + D3.js visualization.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Environment Setup](#environment-setup)
3. [Database Setup](#database-setup)
4. [Data Download](#data-download)
5. [Running the Pipeline](#running-the-pipeline)
6. [Starting the API Server](#starting-the-api-server)
7. [Exporting Outputs](#exporting-outputs)
8. [Frontend Visualization](#frontend-visualization)
9. [Testing & Linting](#testing--linting)
10. [All Make Targets](#all-make-targets)
11. [API Reference](#api-reference)
12. [Project Structure](#project-structure)
13. [Key Design Decisions](#key-design-decisions)

---

## Prerequisites

Make sure the following tools are installed before starting:

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.11 | Runtime |
| Docker & Docker Compose | Latest | PostgreSQL + pgvector *(option A)* |
| PostgreSQL ≥ 16 + pgvector | Latest | Local DB without Docker *(option B)* |
| Node.js & npm | ≥ 18 | Frontend (webui) |
| `curl` | Any | Enron dataset download |

> **Docker not installed?** See [Option B — Local Postgres](#option-b--local-postgres-no-docker) below.  
> To install Docker on Arch Linux: `sudo pacman -S docker` then `sudo systemctl enable --now docker`.

---

## Environment Setup

### 1. Clone the repository

```bash
git clone https://github.com/ShriyaSinha1809/layer10.git
cd layer10/layer10-takehome
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```


### 4. Download the spaCy language model

```bash
python -m spacy download en_core_web_sm
```

### 5. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your API keys:

```bash
# Primary LLM — Groq (free tier: 14,400 req/day)
GROQ_API_KEY=gsk_YOUR_GROQ_KEY_HERE

# Fallback LLM — Google AI Studio (Gemini)
GOOGLE_API_KEY=YOUR_GOOGLE_KEY_HERE
```

All other values (database credentials, model names, batch sizes) have
sensible defaults and do not need to be changed for a standard local run.

> **Get API keys:**
> - Groq: https://console.groq.com/keys
> - Google AI Studio: https://aistudio.google.com/app/apikey

---

## Database Setup

Two options depending on whether Docker is available.

### Option A — Docker (recommended)

```bash
make db
```

This command:
1. Starts the `pgvector/pgvector:pg16` Docker container on port `5432`
2. Waits for Postgres to be healthy
3. Applies `storage/schema.sql` (creates all tables, indexes, extensions)

**Docker not installed yet?** Install it first (Arch Linux):

```bash
sudo pacman -S docker
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out and back in after this
```

Stop and remove the database:

```bash
make db-down      # stops container AND removes the pgdata volume
```

Re-apply schema without restarting:

```bash
make migrate
```

---

### Option B — Local Postgres (no Docker)

Use this if Docker is unavailable. `psql` must already be on your `PATH`
(on Arch Linux, `postgresql` is already installed if `psql` is found).

**1. Ensure the pgvector extension is installed:**

```bash
# Arch Linux
sudo pacman -S pgvector

# Ubuntu / Debian
sudo apt install postgresql-16-pgvector
```

**2. Start the Postgres service (if not already running):**

```bash
sudo systemctl enable --now postgresql
```

**3. Create the database role and apply the schema:**

```bash
make db-local
```

This creates the `layer10` role and database under your local Postgres
instance, then applies `storage/schema.sql`.

**4. Update your `.env`** to use the local connection string (default values
already match, so no change is needed unless your Postgres runs on a
non-standard port):

```bash
DATABASE_URL=postgresql+asyncpg://layer10:layer10pass@localhost:5432/layer10
DATABASE_URL_SYNC=postgresql://layer10:layer10pass@localhost:5432/layer10
```

Tear down the local database:

```bash
make db-local-down
```

Re-apply schema to local Postgres:

```bash
make migrate-local
```

> The schema is **idempotent** — all statements use `IF NOT EXISTS`, so it
> is safe to re-run `make migrate` / `make migrate-local` at any time.

---

## Data Download

Download and extract the Enron email corpus (~1.7 GB):

```bash
make download
```

This fetches the archive from CMU and extracts it to `data/enron_raw/maildir/`.

> **Already have the data?** Set `ENRON_DATA_DIR` in your `.env` to point to
> the existing `maildir/` directory and skip this step.

---

## Running the Pipeline

Run each stage in order:

### Step 1 — Ingest & thread emails

```bash
make ingest
```

Parses raw `.eml` files, deduplicates, builds conversation threads, and
writes structured records into the database.  
Internally runs:
```bash
python -m ingestion.parse_enron
python -m ingestion.thread_builder
```

### Step 2 — LLM extraction

```bash
make extract
```

Sends email batches to Groq (Llama 3.3 70B) with retry + self-correction
feedback. Falls back to Gemini on repeated failure. Extracts entities,
relationships, and claims into the graph tables.  
Internally runs:
```bash
python -m extraction.extractor
```

> **Tip:** Extraction is rate-limited to `EXTRACTION_RATE_LIMIT_RPM=80`
> by default. Adjust in `.env` if your Groq tier allows higher throughput.

---

## Starting the API Server

```bash
make serve
```

Starts a FastAPI server at **http://localhost:8000** with hot-reload enabled.  
Internally runs:
```bash
uvicorn retrieval.api:app --host 0.0.0.0 --port 8000 --reload
```

Interactive Swagger docs: **http://localhost:8000/docs**

---

## Exporting Outputs

### Export the full memory graph

```bash
make export
```

Writes to `outputs/`:
- `outputs/graph_export.json` — full graph (nodes, edges, evidence map)
- `outputs/graph_stats.json` — summary statistics

### Generate example context packs

> Requires the API server to be running (`make serve` in a separate terminal).

```bash
make examples
```

Writes to `outputs/example_queries/`:

```
outputs/example_queries/
├── index.json
├── q1_reporting.json                   # "Who did Kenneth Lay report to?"
├── q2_california_energy.json           # "Key decisions about California energy trading?"
├── q3_skilling_risk.json               # "Who worked with Jeff Skilling on risk management?"
└── q4_trading_strategy_history.json    # "What changed about Enron trading strategy over time?"
```

---

## Frontend Visualization

```bash
cd webui
npm install
npm run dev
```

Opens the React + D3.js graph explorer at **http://localhost:5173**.

> Make sure the API server (`make serve`) is running first so the frontend
> can fetch graph and query data.

---

## Testing & Linting

### Run the test suite

```bash
make test
```

Runs `pytest` with coverage across all test modules in `tests/`.

### Run linting & type-checking

```bash
make lint
```

Runs `ruff check .` (style/imports) and `mypy .` (strict type-checking).

### Clean up caches

```bash
make clean
```

Removes all `__pycache__/` directories and `.pyc` files.

---

## All Make Targets

| Target | Description |
|---|---|
| `make db` | *(Docker)* Start Postgres container, wait for readiness, apply schema |
| `make db-down` | *(Docker)* Stop container and remove the data volume |
| `make migrate` | *(Docker)* Re-apply `storage/schema.sql` to the running container |
| `make db-local` | *(Local)* Create role + DB in local Postgres, apply schema |
| `make db-local-down` | *(Local)* Drop the local `layer10` database and role |
| `make migrate-local` | *(Local)* Re-apply `storage/schema.sql` to local Postgres |
| `make download` | Download & extract the Enron dataset |
| `make ingest` | Parse emails + build threads into the database |
| `make extract` | Run LLM extraction (entities, claims, evidence) |
| `make serve` | Start the FastAPI server on port 8000 |
| `make export` | Export graph snapshot to `outputs/` |
| `make examples` | Generate example context-pack JSON files |
| `make test` | Run pytest with coverage |
| `make lint` | Run ruff + mypy |
| `make clean` | Remove `__pycache__` and `.pyc` files |

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/query?q=` | Natural language query → grounded context pack |
| `GET` | `/api/query?q=&user_id=` | Same, with permissions filter |
| `GET` | `/api/entity/{id}` | Entity details + aliases |
| `GET` | `/api/entity/{id}/claims` | All claims for an entity |
| `GET` | `/api/entity/{id}/merges` | Full merge audit trail |
| `GET` | `/api/claim/{id}/evidence` | Evidence supporting a claim |
| `GET` | `/api/review-queue` | Claims flagged for human review (confidence 0.4–0.5) |
| `GET` | `/api/graph` | Graph data for visualization |
| `GET` | `/api/metrics` | Detailed observability metrics (quality, volume, temporal) |
| `GET` | `/api/stats` | Summary counts |

Interactive docs: **http://localhost:8000/docs**

---

## Project Structure

```
layer10-takehome/
├── .env.example           # Environment variable template
├── config.py              # Centralized settings (pydantic-settings)
├── logging_config.py      # Structured logging setup
├── pipeline.py            # End-to-end pipeline orchestrator
├── Makefile               # All dev/ops commands
├── pyproject.toml         # Project metadata and dependencies
├── requirements.txt       # Pinned dependencies
├── docker-compose.yml     # PostgreSQL + pgvector service
│
├── ingestion/             # Email parsing, dedup, thread building
├── extraction/            # LLM extraction, prompts, schema, validation
├── dedup/                 # Entity resolution, claim dedup, merge audit
├── storage/               # SQLAlchemy models, schema.sql, pgvector embeddings
├── retrieval/             # FastAPI app, graph traversal, context packs
├── visualization/         # D3.js helpers
├── webui/                 # React + Vite frontend
├── scripts/               # One-off scripts (export, seed, examples)
├── tests/                 # Pytest test suite
├── notebooks/             # Exploratory notebooks
└── docs/                  # Architecture write-up and screenshots
```

---

## Key Design Decisions

- **PostgreSQL over Neo4j:** Simpler deployment; adjacency tables sufficient at this scale; pgvector enables hybrid semantic + keyword search in a single store.
- **Evidence-first extraction:** Every claim must have a verifiable text excerpt — memory without grounding is hallucination.
- **Temporal validity windows:** Claims track `[valid_from, valid_to)` on `WORKS_AT`/`REPORTS_TO`; the pipeline automatically closes a claim's window when a conflicting one arrives.
- **Pending review queue:** Claims with confidence 0.4–0.5 are stored but flagged `pending_review=true`, surfaced via `GET /api/review-queue`.
- **Reversible merges:** Entity resolution errors are undoable via an audit trail (`merge_events` table); full history is visible in the UI.
- **Hybrid retrieval:** Keyword + embedding + fuzzy matching for robust coverage across name variations and paraphrases.
- **LLM retry loop:** On parse or validation failure, the extractor appends error feedback to the prompt and retries (up to `EXTRACTION_MAX_RETRIES`), then falls back to the secondary provider.
