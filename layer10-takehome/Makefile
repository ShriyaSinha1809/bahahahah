.PHONY: db db-down migrate db-local db-local-down migrate-local \
        ingest extract serve test lint clean download export examples

# ─── Database — Docker (default) ───────────────────────────
# Requires: docker + docker compose
db:
	@command -v docker >/dev/null 2>&1 || \
		{ echo "ERROR: 'docker' not found. Install it (see README) or use 'make db-local' instead."; exit 1; }
	docker compose up -d postgres
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec postgres pg_isready -U layer10 2>/dev/null; do sleep 1; done
	@echo "Postgres is ready. Applying schema..."
	$(MAKE) migrate
	@echo "Schema applied."

db-down:
	docker compose down -v

# ─── Schema Migration — Docker ─────────────────────────────
# Applies storage/schema.sql to the running container.
# Safe to re-run (all statements use IF NOT EXISTS).
migrate:
	docker compose exec -T postgres psql \
		-U layer10 -d layer10 \
		-v ON_ERROR_STOP=0 \
		< storage/schema.sql
	@echo "Migration complete."

# ─── Database — Local Postgres (no Docker) ─────────────────
# Use this when Docker is not available.
# Requires: postgresql + postgresql-contrib (with pgvector extension)
# Arch:   sudo pacman -S postgresql pgvector
# Ubuntu: sudo apt install postgresql postgresql-contrib
#         then install pgvector: https://github.com/pgvector/pgvector
db-local:
	@command -v psql >/dev/null 2>&1 || \
		{ echo "ERROR: 'psql' not found. Install postgresql first."; exit 1; }
	@echo "Creating role and database if they do not exist..."
	@sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='layer10'" | grep -q 1 || \
		sudo -u postgres psql -c "CREATE ROLE layer10 LOGIN PASSWORD 'layer10pass';"
	@sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='layer10'" | grep -q 1 || \
		sudo -u postgres psql -c "CREATE DATABASE layer10 OWNER layer10;"
	$(MAKE) migrate-local
	@echo "Local Postgres setup complete."

db-local-down:
	@sudo -u postgres psql -c "DROP DATABASE IF EXISTS layer10;"
	@sudo -u postgres psql -c "DROP ROLE IF EXISTS layer10;"
	@echo "Local database and role removed."

# ─── Schema Migration — Local Postgres ─────────────────────
migrate-local:
	psql -U layer10 -d layer10 -v ON_ERROR_STOP=0 < storage/schema.sql
	@echo "Migration complete."

# ─── Data Download ─────────────────────────────────────────
download:
	@echo "Downloading Enron dataset..."
	mkdir -p data
	cd data && curl -L -o enron_mail.tar.gz \
		"https://www.cs.cmu.edu/~enron/enron_mail_20110402.tar.gz"
	cd data && tar -xzf enron_mail.tar.gz
	@echo "Dataset extracted to data/enron_raw/"

# ─── Ingestion ─────────────────────────────────────────────
ingest:
	python -m ingestion.parse_enron
	python -m ingestion.thread_builder

# ─── Extraction ────────────────────────────────────────────
extract:
	python -m extraction.extractor

# ─── API Server ────────────────────────────────────────────
serve:
	uvicorn retrieval.api:app --host 0.0.0.0 --port 8000 --reload

# ─── Outputs ───────────────────────────────────────────────
# Export the full memory graph to outputs/graph_export.json
export:
	mkdir -p outputs
	python scripts/export_graph.py --out outputs/

# Generate example context-pack JSON files (requires API to be running)
# Run 'make serve' in another terminal first.
examples:
	mkdir -p outputs/example_queries
	python scripts/generate_examples.py --api http://localhost:8000

# ─── Quality ───────────────────────────────────────────────
test:
	pytest tests/ -v --cov

lint:
	ruff check .
	mypy .

# ─── Cleanup ───────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
