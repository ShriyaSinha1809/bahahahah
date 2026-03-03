.PHONY: db db-down ingest extract serve test lint clean download

# ─── Database ──────────────────────────────────────────────
db:
	docker compose up -d postgres
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec postgres pg_isready -U layer10 2>/dev/null; do sleep 1; done
	@echo "Postgres is ready."

db-down:
	docker compose down -v

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
