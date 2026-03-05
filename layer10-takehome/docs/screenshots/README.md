# Screenshots

This directory contains screenshots of the running UI.

## How to Capture

With the full pipeline complete (`make db && make ingest && make extract`)
and the API + UI running (`make serve` + `cd webui && npm run dev`):

| Screenshot | What to capture |
|---|---|
| `01_graph_overview.png` | Full graph view — all entities + edges, no filter |
| `02_graph_filtered.png` | Graph filtered to Person + Organization entities, confidence ≥ 0.5 |
| `03_evidence_panel.png` | Click a claim edge → Evidence Panel open showing verbatim excerpt + source metadata |
| `04_merge_inspector.png` | Click an entity → Merge Inspector tab showing aliases + full merge history |
| `05_query_result.png` | Type a question (e.g. "Who did Kenneth Lay report to?") → result highlighted in graph |
| `06_review_queue.png` | (Optional) `/api/review-queue` response in browser or Swagger UI showing pending claims |

## Quick Capture (Linux)
```bash
# Gnome Screenshot
gnome-screenshot -w -f docs/screenshots/01_graph_overview.png

# Flameshot
flameshot gui --path docs/screenshots/
```
