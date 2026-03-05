"""
Generate example context packs by running sample questions through the
retrieval API and saving the JSON responses to outputs/example_queries/.

This script is run after the pipeline has been fully executed (ingest + extract
+ canonicalize). It demonstrates that every retrieved memory item is grounded
in source evidence.

Usage:
    python scripts/generate_examples.py
    python scripts/generate_examples.py --api http://localhost:8000

The four example questions map to key evaluation scenarios:
  Q1 — Reporting relationship lookup (REPORTS_TO)
  Q2 — Topic-based multi-entity retrieval (DISCUSSES / DECIDED)
  Q3 — Multi-hop collaboration network (PARTICIPATES_IN + WORKS_AT)
  Q4 — Temporal evolution of strategy (historical claims, is_current filter)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

OUT_DIR = Path("outputs/example_queries")

EXAMPLE_QUESTIONS = [
    {
        "id": "q1_reporting",
        "question": "Who did Kenneth Lay report to?",
        "depth": 2,
        "min_confidence": 0.4,
        "notes": "Tests REPORTS_TO claim resolution and Person entity linking.",
    },
    {
        "id": "q2_california_energy",
        "question": "What were the key decisions about California energy trading?",
        "depth": 2,
        "min_confidence": 0.4,
        "notes": "Tests DECIDED + DISCUSSES claim retrieval; topic-based expansion.",
    },
    {
        "id": "q3_skilling_risk",
        "question": "Who worked with Jeff Skilling on risk management?",
        "depth": 2,
        "min_confidence": 0.4,
        "notes": "Multi-hop: PARTICIPATES_IN + WORKS_AT across Person/Project/Topic.",
    },
    {
        "id": "q4_trading_strategy_history",
        "question": "What changed about Enron trading strategy over time?",
        "depth": 2,
        "min_confidence": 0.3,
        "notes": "Temporal evolution test: historical claims + is_current distinction.",
    },
]


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


async def generate_examples(api_base: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(base_url=api_base, timeout=60.0) as client:
        # Verify API is reachable
        try:
            resp = await client.get("/health")
            resp.raise_for_status()
            print(f"✓  API reachable at {api_base}")
        except Exception as exc:
            print(f"✗  Cannot reach API at {api_base}: {exc}")
            print("   Run 'make serve' first, then re-run this script.")
            sys.exit(1)

        for item in EXAMPLE_QUESTIONS:
            qid = item["id"]
            question = item["question"]
            print(f"\n[{qid}] {question}")

            try:
                resp = await client.get(
                    "/api/query",
                    params={
                        "q": question,
                        "depth": item["depth"],
                        "min_confidence": item["min_confidence"],
                        "include_historical": True,
                    },
                )
                resp.raise_for_status()
                context_pack = resp.json()

                output = {
                    "metadata": {
                        "question_id": qid,
                        "question": question,
                        "notes": item["notes"],
                        "generated_at": datetime.utcnow().isoformat(),
                        "api_params": {
                            "depth": item["depth"],
                            "min_confidence": item["min_confidence"],
                        },
                    },
                    "context_pack": context_pack,
                    "summary": {
                        "entities_found": len(context_pack.get("entities", [])),
                        "claims_found": len(context_pack.get("claims", [])),
                        "conflicts_found": len(context_pack.get("conflicts", [])),
                        "total_evidence": context_pack.get("total_evidence_count", 0),
                    },
                }

                out_path = OUT_DIR / f"{qid}.json"
                out_path.write_text(
                    json.dumps(output, indent=2, default=_serialize), encoding="utf-8"
                )

                summary = output["summary"]
                print(f"   ✓  {summary['entities_found']} entities  "
                      f"{summary['claims_found']} claims  "
                      f"{summary['total_evidence']} evidence  "
                      f"→ {out_path}")

            except httpx.HTTPStatusError as exc:
                print(f"   ✗  HTTP {exc.response.status_code}: {exc.response.text[:200]}")
            except Exception as exc:
                print(f"   ✗  {exc}")

    # Write an index
    index = {
        "generated_at": datetime.utcnow().isoformat(),
        "api_base": api_base,
        "questions": [
            {
                "id": q["id"],
                "question": q["question"],
                "file": f"{q['id']}.json",
                "notes": q["notes"],
            }
            for q in EXAMPLE_QUESTIONS
        ],
    }
    index_path = OUT_DIR / "index.json"
    index_path.write_text(
        json.dumps(index, indent=2, default=_serialize), encoding="utf-8"
    )
    print(f"\n✓  Index written to {index_path}")
    print(f"   All context packs in {OUT_DIR}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate example context packs from the retrieval API"
    )
    parser.add_argument(
        "--api",
        default="http://localhost:8000",
        help="Base URL of the running FastAPI server (default: http://localhost:8000)",
    )
    args = parser.parse_args()
    asyncio.run(generate_examples(args.api))


if __name__ == "__main__":
    main()
