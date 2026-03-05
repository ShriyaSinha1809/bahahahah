// ─── Layer10 Memory Graph API Client ──────────────────────────

const BASE = "";

// ─── Types ────────────────────────────────────────────────────

export interface EntitySummary {
  entity_id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
  properties: Record<string, unknown>;
}

export interface EvidenceSnippet {
  source_id: string;
  excerpt: string;
  source_date: string | null;
  sender: string;
  subject: string;
  extraction_version: string;
}

export interface ClaimWithEvidence {
  claim_id: string;
  claim_type: string;
  subject: string;
  object: string;
  properties: Record<string, unknown>;
  confidence: number;
  valid_from: string | null;
  valid_to: string | null;
  is_current: boolean;
  evidence: EvidenceSnippet[];
}

export interface EntityDetail {
  entity_id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
  properties: Record<string, unknown>;
  claim_count: number;
}

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  aliases: string[];
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  confidence: number;
  label: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface StatsResponse {
  total_emails: number;
  total_entities: number;
  total_claims: number;
  total_evidence: number;
}

export interface MergeEvent {
  event_id: string;
  action_type: string;
  source_ids: string[];
  target_id: string;
  reason: string;
  confidence: number | null;
  created_at: string | null;
  reversed_at: string | null;
  reversed_reason: string | null;
}

export interface MetricsResponse {
  total_emails: number;
  total_entities: number;
  total_claims: number;
  total_evidence: number;
  total_merges: number;
  pending_review_claims: number;
  failed_extractions: number;
  completed_extractions: number;
  avg_confidence: number;
  low_confidence_claims: number;
  high_confidence_claims: number;
  historical_claims: number;
  current_claims: number;
  reversed_merges: number;
}

export interface ContextPack {
  question: string;
  entities: EntitySummary[];
  claims: ClaimWithEvidence[];
  conflicts: unknown[];
  total_evidence_count: number;
}

// ─── API Calls ────────────────────────────────────────────────

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`);
  return res.json();
}

export const api = {
  health: () => get<{ status: string }>("/health"),
  stats: () => get<StatsResponse>("/api/stats"),
  graph: (params?: { center_entity?: string; depth?: number; min_confidence?: number }) => {
    const q = new URLSearchParams();
    if (params?.center_entity) q.set("center_entity", params.center_entity);
    if (params?.depth) q.set("depth", String(params.depth));
    if (params?.min_confidence) q.set("min_confidence", String(params.min_confidence));
    const qs = q.toString();
    return get<GraphData>(`/api/graph${qs ? `?${qs}` : ""}`);
  },
  entity: (id: string) => get<EntityDetail>(`/api/entity/${id}`),
  entityClaims: (id: string, claimType?: string) => {
    const q = new URLSearchParams();
    if (claimType) q.set("claim_type", claimType);
    q.set("current_only", "false");
    return get<ClaimWithEvidence[]>(`/api/entity/${id}/claims?${q}`);
  },
  entityMerges: (id: string) => get<MergeEvent[]>(`/api/entity/${id}/merges`),
  claimEvidence: (id: string) => get<EvidenceSnippet[]>(`/api/claim/${id}/evidence`),
  reviewQueue: (limit = 50) => get<ClaimWithEvidence[]>(`/api/review-queue?limit=${limit}`),
  metrics: () => get<MetricsResponse>("/api/metrics"),
  query: (question: string, depth = 2, minConfidence = 0.5) =>
    get<ContextPack>(
      `/api/query?q=${encodeURIComponent(question)}&depth=${depth}&min_confidence=${minConfidence}`
    ),
};
