import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { api, type GraphData, type StatsResponse, type GraphNode, type MetricsResponse, type ClaimWithEvidence } from "./api";
import { typeColor } from "./colors";
import GraphView from "./components/GraphView";
import EvidencePanel from "./components/EvidencePanel";
import MergeInspector from "./components/MergeInspector";
import TimelinePanel from "./components/TimelinePanel";
import {
  Brain,
  Database,
  Users,
  FileText,
  Link2,
  Search,
  Shield,
  Combine,
  Send,
  Clock,
  BarChart2,
  ListChecks,
  ArrowLeft,
  AlertTriangle,
  X,
} from "lucide-react";
import "./App.css";

export default function App() {
  // ── State ──────────────────────────────────────────
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [allNodes, setAllNodes] = useState<GraphNode[]>([]);
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [activeTypes, setActiveTypes] = useState<Set<string>>(new Set());
  const [minConfidence, setMinConfidence] = useState(0.3);
  const [rightTab, setRightTab] = useState<"evidence" | "merge" | "timeline" | "metrics" | "queue">("evidence");
  const [query, setQuery] = useState("");
  const [querying, setQuerying] = useState(false);
  const [isQueryView, setIsQueryView] = useState(false);
  const [metricsData, setMetricsData] = useState<MetricsResponse | null>(null);
  const [reviewQueue, setReviewQueue] = useState<ClaimWithEvidence[]>([]);
  const [toasts, setToasts] = useState<{ id: number; msg: string }[]>([]);

  // base graph is saved so query results can be cleared
  const baseGraphRef = useRef<{ graph: GraphData; nodes: GraphNode[] } | null>(null);
  const toastIdRef = useRef(0);

  const addToast = useCallback((msg: string) => {
    const id = ++toastIdRef.current;
    setToasts((prev) => [...prev, { id, msg }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  // ── Derive unique entity types from loaded data ────
  const knownTypes = useMemo(() => {
    const types = new Set(allNodes.map((n) => n.type));
    return Array.from(types).sort();
  }, [allNodes]);

  // Initialize activeTypes when data first loads
  useEffect(() => {
    if (knownTypes.length > 0 && activeTypes.size === 0) {
      setActiveTypes(new Set(knownTypes));
    }
  }, [knownTypes, activeTypes.size]);

  // ── Load initial data ──────────────────────────────
  useEffect(() => {
    api.stats().then(setStats).catch(() => addToast("Failed to load stats"));
    api.graph({ min_confidence: 0.1, depth: 2 }).then((g) => {
      setGraphData(g);
      setAllNodes(g.nodes);
      baseGraphRef.current = { graph: g, nodes: g.nodes };
    }).catch(() => addToast("Failed to load graph"));
  }, [addToast]);

  // ── Filter graph data ──────────────────────────────
  const filteredGraph = useMemo<GraphData | null>(() => {
    if (!graphData) return null;
    const nodes = graphData.nodes.filter((n) => activeTypes.has(n.type));
    const nodeIds = new Set(nodes.map((n) => n.id));
    const edges = graphData.edges.filter(
      (e) =>
        nodeIds.has(e.source) &&
        nodeIds.has(e.target) &&
        e.confidence >= minConfidence
    );
    return { nodes, edges };
  }, [graphData, activeTypes, minConfidence]);

  // ── Sidebar entity list ────────────────────────────
  const entityList = useMemo(() => {
    let list = allNodes.filter((n) => activeTypes.has(n.type));
    if (search.trim()) {
      const s = search.toLowerCase();
      list = list.filter(
        (n) =>
          n.label.toLowerCase().includes(s) ||
          n.aliases.some((a) => a.toLowerCase().includes(s))
      );
    }
    return list.sort((a, b) => a.label.localeCompare(b.label));
  }, [allNodes, activeTypes, search]);

  // ── Handlers ───────────────────────────────────────
  const toggleType = (t: string) => {
    setActiveTypes((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  };

  const handleSelectEntity = useCallback((id: string | null) => {
    setSelectedEntity(id);
    if (id) setRightTab("evidence");
  }, []);

  const handleResetToFullGraph = useCallback(() => {
    if (baseGraphRef.current) {
      setGraphData(baseGraphRef.current.graph);
      setAllNodes(baseGraphRef.current.nodes);
      setIsQueryView(false);
    }
  }, []);

  const handleLoadMetrics = useCallback(async () => {
    try {
      const m = await api.metrics();
      setMetricsData(m);
    } catch {
      addToast("Failed to load metrics");
    }
  }, [addToast]);

  const handleLoadQueue = useCallback(async () => {
    try {
      const q = await api.reviewQueue(100);
      setReviewQueue(q);
    } catch {
      addToast("Failed to load review queue");
    }
  }, [addToast]);

  const handleQuery = async () => {
    if (!query.trim()) return;
    setQuerying(true);
    try {
      const pack = await api.query(query, 2, minConfidence);
      // Build graph from context pack
      const nodesMap = new Map<string, GraphNode>();
      const nameToId = new Map<string, string>();
      for (const e of pack.entities) {
        nodesMap.set(e.entity_id, {
          id: e.entity_id,
          label: e.canonical_name,
          type: e.entity_type,
          aliases: e.aliases,
        });
        nameToId.set(e.canonical_name.toLowerCase(), e.entity_id);
        for (const a of e.aliases) {
          nameToId.set(a.toLowerCase(), e.entity_id);
        }
      }
      const edges = pack.claims.map((c) => {
        const srcId = nodesMap.has(c.subject) ? c.subject : nameToId.get(c.subject.toLowerCase());
        const tgtId = nodesMap.has(c.object) ? c.object : nameToId.get(c.object.toLowerCase());
        return {
          id: c.claim_id,
          source: srcId ?? c.subject,
          target: tgtId ?? c.object,
          type: c.claim_type,
          confidence: c.confidence,
          label: c.claim_type,
        };
      }).filter((e) => nodesMap.has(e.source) && nodesMap.has(e.target));

      const nodes = Array.from(nodesMap.values());
      setGraphData({ nodes, edges });
      setAllNodes((prev) => {
        const existing = new Set(prev.map((n) => n.id));
        const merged = [...prev];
        for (const n of nodes) {
          if (!existing.has(n.id)) merged.push(n);
        }
        return merged;
      });
      setIsQueryView(true);
    } catch {
      addToast(`Query failed: "${query}"`);
    }
    setQuerying(false);
  };

  // ── Render ─────────────────────────────────────────
  return (
    <div className="app">
      {/* Toast Notifications */}
      {toasts.length > 0 && (
        <div className="toast-container">
          {toasts.map((t) => (
            <div key={t.id} className="toast">
              <AlertTriangle size={13} />
              <span>{t.msg}</span>
              <button
                onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
                style={{ background: "none", border: "none", cursor: "pointer", color: "inherit", padding: 0, marginLeft: "auto" }}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
      {/* Top Bar */}
      <header className="topbar">
        <div className="topbar-brand">
          <Brain size={20} />
          <h1>Layer10 Memory Graph</h1>
        </div>
        {stats && (
          <div className="topbar-stats">
            <span className="stat-item">
              <Database size={12} />
              <span className="stat-value">{stats.total_emails}</span> emails
            </span>
            <span className="stat-item">
              <Users size={12} />
              <span className="stat-value">{stats.total_entities}</span> entities
            </span>
            <span className="stat-item">
              <Link2 size={12} />
              <span className="stat-value">{stats.total_claims}</span> claims
            </span>
            <span className="stat-item">
              <FileText size={12} />
              <span className="stat-value">{stats.total_evidence}</span> evidence
            </span>
          </div>
        )}
      </header>

      {/* Sidebar */}
      <aside className="sidebar">
        {/* Search */}
        <div className="sidebar-header">
          <h2>Entities</h2>
          <input
            className="search-box"
            placeholder="Filter entities…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* Query */}
        <div className="query-section">
          <div className="query-input-wrap">
            <input
              className="query-input"
              placeholder="Ask a question…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleQuery()}
            />
            <button className="query-btn" onClick={handleQuery} disabled={querying}>
              {querying ? <span className="spinner" style={{ width: 12, height: 12, margin: 0 }} /> : <Send size={12} />}
            </button>
          </div>
          {isQueryView && (
            <button
              className="back-btn"
              onClick={handleResetToFullGraph}
              title="Return to the full graph"
            >
              <ArrowLeft size={11} /> Back to full graph
            </button>
          )}
        </div>

        {/* Filters */}
        <div className="filters">
          <div className="filter-group">
            <label>Type</label>
            {knownTypes.map((t) => (
              <button
                key={t}
                className={`filter-chip ${activeTypes.has(t) ? "active" : ""}`}
                onClick={() => toggleType(t)}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="filter-group">
            <label>Min Confidence</label>
            <div className="confidence-slider">
              <input
                type="range"
                min="0"
                max="100"
                value={Math.round(minConfidence * 100)}
                onChange={(e) => setMinConfidence(Number(e.target.value) / 100)}
              />
              <span>{Math.round(minConfidence * 100)}%</span>
            </div>
          </div>
        </div>

        {/* Entity List */}
        <div className="entity-list">
          {entityList.map((node) => (
            <div
              key={node.id}
              className={`entity-item ${selectedEntity === node.id ? "selected" : ""}`}
              onClick={() => handleSelectEntity(node.id)}
            >
              <span className="entity-dot" style={{ background: typeColor(node.type) }} />
              <div className="entity-info">
                <div className="entity-name">{node.label}</div>
                <div className="entity-type">{node.type}</div>
              </div>
            </div>
          ))}
          {entityList.length === 0 && (
            <div className="empty-state" style={{ padding: "30px 10px" }}>
              <Search size={20} />
              <p>No entities match</p>
            </div>
          )}
        </div>
      </aside>

      {/* Graph */}
      <GraphView
        data={filteredGraph}
        selectedEntity={selectedEntity}
        onSelectEntity={handleSelectEntity}
      />

      {/* Right Panel */}
      <div className="right-panel">
        <div className="panel-tabs">
          <button
            className={`panel-tab ${rightTab === "evidence" ? "active" : ""}`}
            onClick={() => setRightTab("evidence")}
          >
            <Shield size={13} /> Evidence
          </button>
          <button
            className={`panel-tab ${rightTab === "merge" ? "active" : ""}`}
            onClick={() => setRightTab("merge")}
          >
            <Combine size={13} /> Merges
          </button>
          <button
            className={`panel-tab ${rightTab === "timeline" ? "active" : ""}`}
            onClick={() => setRightTab("timeline")}
          >
            <Clock size={13} /> Timeline
          </button>
          <button
            className={`panel-tab ${rightTab === "metrics" ? "active" : ""}`}
            onClick={() => { setRightTab("metrics"); handleLoadMetrics(); }}
          >
            <BarChart2 size={13} /> Metrics
          </button>
          <button
            className={`panel-tab ${rightTab === "queue" ? "active" : ""}`}
            onClick={() => { setRightTab("queue"); handleLoadQueue(); }}
          >
            <ListChecks size={13} /> Queue
          </button>
        </div>
        <div className="panel-content">
          {rightTab === "evidence" && <EvidencePanel entityId={selectedEntity} />}
          {rightTab === "merge" && <MergeInspector entityId={selectedEntity} />}
          {rightTab === "timeline" && <TimelinePanel entityId={selectedEntity} />}
          {rightTab === "metrics" && (
            <div style={{ padding: "8px 4px" }}>
              {!metricsData ? (
                <div className="empty-state"><BarChart2 size={28} /><p>Loading metrics…</p></div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <MetricRow label="Emails" value={metricsData.total_emails} />
                  <MetricRow label="Entities" value={metricsData.total_entities} />
                  <MetricRow label="Claims" value={metricsData.total_claims} />
                  <MetricRow label="Evidence" value={metricsData.total_evidence} />
                  <MetricRow label="Merges" value={metricsData.total_merges} accent />
                  <hr style={{ border: "none", borderTop: "1px solid var(--border)", margin: "4px 0" }} />
                  <MetricRow label="Current Claims" value={metricsData.current_claims} />
                  <MetricRow label="Historical" value={metricsData.historical_claims} dim />
                  <MetricRow label="Avg Confidence" value={`${(metricsData.avg_confidence * 100).toFixed(1)}%`} />
                  <MetricRow label="High Confidence" value={metricsData.high_confidence_claims} />
                  <MetricRow label="Pending Review" value={metricsData.pending_review_claims} accent />
                  <MetricRow label="Failed Extractions" value={metricsData.failed_extractions} dim />
                  <MetricRow label="Completed Extractions" value={metricsData.completed_extractions} />
                  <MetricRow label="Reversed Merges" value={metricsData.reversed_merges} dim />
                </div>
              )}
            </div>
          )}
          {rightTab === "queue" && (
            <div style={{ padding: "4px 0" }}>
              {reviewQueue.length === 0 ? (
                <div className="empty-state">
                  <ListChecks size={28} />
                  <p>Review queue is empty</p>
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>
                    {reviewQueue.length} claim{reviewQueue.length !== 1 ? "s" : ""} pending review
                  </div>
                  {reviewQueue.map((claim) => (
                    <div key={claim.claim_id} className="merge-section" style={{ cursor: "default" }}>
                      <div style={{ fontWeight: 600, fontSize: 12, color: "var(--text-primary)", marginBottom: 3 }}>
                        {claim.subject} → {claim.object}
                      </div>
                      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                        <span className="type-badge" style={{ fontSize: 10 }}>{claim.claim_type}</span>
                        <span style={{ fontSize: 11, color: "#f59e0b" }}>
                          {(claim.confidence * 100).toFixed(0)}% confidence
                        </span>
                        <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: "auto" }}>
                          {claim.evidence.length} src
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Metric display helper ─────────────────────────────────────
function MetricRow({
  label,
  value,
  accent = false,
  dim = false,
}: {
  label: string;
  value: string | number;
  accent?: boolean;
  dim?: boolean;
}) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
      <span style={{ color: dim ? "var(--text-muted)" : "var(--text-secondary)" }}>{label}</span>
      <span style={{ fontWeight: 600, color: accent ? "var(--accent)" : dim ? "var(--text-muted)" : "var(--text-primary)" }}>
        {value}
      </span>
    </div>
  );
}
