"""
Export the memory graph to JSON for submission / offline inspection.

Produces two files in outputs/:
  graph_export.json     — full graph: nodes (entities) + edges (claims) + evidence map
  graph_stats.json      — summary statistics about the exported graph

Usage:
    python scripts/export_graph.py
    python scripts/export_graph.py --min-confidence 0.5 --out outputs/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import (
    ClaimRepository,
    EntityRepository,
    EvidenceRepository,
    get_session,
    init_db,
    close_db,
)
from logging_config import get_logger, setup_logging

logger = get_logger(__name__)

def _serialize(obj):
    """JSON serializer that handles datetime and UUID objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

async def export_graph(
    min_confidence: float = 0.0,
    include_historical: bool = True,
    out_dir: Path = Path("outputs"),
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    await init_db()
    logger.info("export_start", min_confidence=min_confidence)

    async with get_session() as session:
        entities = await EntityRepository.list_all(session, limit=100_000)
        logger.info("entities_loaded", count=len(entities))

        entity_index: dict[str, dict] = {}
        nodes: list[dict] = []
        for e in entities:
            eid = str(e["id"])
            node = {
                "id": eid,
                "canonical_name": e["canonical_name"],
                "entity_type": e["entity_type"],
                "aliases": e.get("aliases") or [],
                "properties": e.get("properties") or {},
                "created_at": e.get("created_at"),
            }
            nodes.append(node)
            entity_index[eid] = node

        result = await session.execute(
            text("""
                SELECT c.*,
                       s.canonical_name AS subject_name,
                       o.canonical_name AS object_name
                FROM claims c
                JOIN entities s ON c.subject_id = s.id
                JOIN entities o ON c.object_id = o.id
                WHERE c.confidence >= :min_conf
                ORDER BY c.confidence DESC, c.created_at DESC
            """),
            {"min_conf": min_confidence},
        )
        claim_rows = [dict(r._mapping) for r in result.fetchall()]
        logger.info("claims_loaded", count=len(claim_rows))

        evidence_map: dict[str, list[dict]] = {}
        for claim in claim_rows:
            cid = str(claim["id"])
            ev_rows = await EvidenceRepository.get_for_claim(session, cid)
            evidence_map[cid] = [
                {
                    "source_id": ev.get("source_id", ""),
                    "excerpt": ev.get("excerpt", ""),
                    "source_timestamp": ev.get("source_timestamp"),
                    "sender": ev.get("sender", ""),
                    "email_subject": ev.get("email_subject", ""),
                    "extraction_version": ev.get("extraction_version", ""),
                    "confidence": ev.get("confidence"),
                }
                for ev in ev_rows
            ]

        edges: list[dict] = [
            {
                "id": str(c["id"]),
                "claim_type": c["claim_type"],
                "source": str(c["subject_id"]),
                "target": str(c["object_id"]),
                "subject_name": c.get("subject_name", ""),
                "object_name": c.get("object_name", ""),
                "properties": c.get("properties") or {},
                "confidence": c["confidence"],
                "valid_from": c.get("valid_from"),
                "valid_to": c.get("valid_to"),
                "is_current": c.get("is_current", True),
                "pending_review": c.get("pending_review", False),
                "created_at": c.get("created_at"),
            }
            for c in claim_rows
        ]

    await close_db()

    graph = {
        "metadata": {
            "exported_at": datetime.utcnow().isoformat(),
            "min_confidence": min_confidence,
            "include_historical": include_historical,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "evidence_count": sum(len(v) for v in evidence_map.values()),
        },
        "nodes": nodes,
        "edges": edges,
        "evidence_map": evidence_map,
    }
    graph_path = out_dir / "graph_export.json"
    graph_path.write_text(
        json.dumps(graph, indent=2, default=_serialize), encoding="utf-8"
    )
    logger.info("graph_exported", path=str(graph_path), nodes=len(nodes), edges=len(edges))

    type_counts: dict[str, int] = {}
    for n in nodes:
        t = n["entity_type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    claim_type_counts: dict[str, int] = {}
    for e in edges:
        t = e["claim_type"]
        claim_type_counts[t] = claim_type_counts.get(t, 0) + 1

    current_edges = [e for e in edges if e["is_current"]]
    pending_edges = [e for e in edges if e["pending_review"]]
    all_confidences = [e["confidence"] for e in edges]
    avg_conf = sum(all_confidences) / len(all_confidences) if all_confidences else 0.0

    stats = {
        "exported_at": graph["metadata"]["exported_at"],
        "node_count": len(nodes),
        "edge_count": len(edges),
        "current_edge_count": len(current_edges),
        "historical_edge_count": len(edges) - len(current_edges),
        "pending_review_count": len(pending_edges),
        "avg_confidence": round(avg_conf, 4),
        "entity_type_counts": type_counts,
        "claim_type_counts": claim_type_counts,
        "total_evidence_snippets": graph["metadata"]["evidence_count"],
    }
    stats_path = out_dir / "graph_stats.json"
    stats_path.write_text(
        json.dumps(stats, indent=2, default=_serialize), encoding="utf-8"
    )
    logger.info("stats_exported", path=str(stats_path))
    print(f"\n✓  Graph export complete")
    print(f"   {len(nodes):,} entities  |  {len(edges):,} claims  |  "
          f"{graph['metadata']['evidence_count']:,} evidence snippets")
    print(f"   Output: {graph_path}")
    print(f"   Stats:  {stats_path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Export memory graph to JSON")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="Minimum claim confidence to include (default: 0.0)")
    parser.add_argument("--out", type=Path, default=Path("outputs"),
                        help="Output directory (default: outputs/)")
    args = parser.parse_args()

    setup_logging()
    asyncio.run(export_graph(
        min_confidence=args.min_confidence,
        out_dir=args.out,
    ))

if __name__ == "__main__":
    main()
