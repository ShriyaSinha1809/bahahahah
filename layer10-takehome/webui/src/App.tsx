import { useState, useEffect, useCallback, useMemo } from "react";
import { api, type GraphData, type StatsResponse, type GraphNode } from "./api";
import { typeColor } from "./colors";
import GraphView from "./components/GraphView";
import EvidencePanel from "./components/EvidencePanel";
import MergeInspector from "./components/MergeInspector";
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
  const [rightTab, setRightTab] = useState<"evidence" | "merge">("evidence");
  const [query, setQuery] = useState("");
  const [querying, setQuerying] = useState(false);

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
    api.stats().then(setStats).catch(console.error);
    api.graph({ min_confidence: 0.1, depth: 2 }).then((g) => {
      setGraphData(g);
      setAllNodes(g.nodes);
    }).catch(console.error);
  }, []);

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
        // Map canonical name + aliases to ID for edge resolution
        nameToId.set(e.canonical_name.toLowerCase(), e.entity_id);
        for (const a of e.aliases) {
          nameToId.set(a.toLowerCase(), e.entity_id);
        }
      }
      const edges = pack.claims.map((c) => {
        // subject/object may be names OR ids
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
    } catch (err) {
      console.error("Query failed:", err);
    }
    setQuerying(false);
  };

  // ── Render ─────────────────────────────────────────
  return (
    <div className="app">
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
        </div>
        <div className="panel-content">
          {rightTab === "evidence" ? (
            <EvidencePanel entityId={selectedEntity} />
          ) : (
            <MergeInspector entityId={selectedEntity} />
          )}
        </div>
      </div>
    </div>
  );
}
