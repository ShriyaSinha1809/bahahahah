import { useState, useEffect } from "react";
import { api, type TimelineEntry } from "../api";
import { Clock, CheckCircle, Archive } from "lucide-react";

interface Props {
  entityId: string | null;
}

const TYPE_COLORS: Record<string, string> = {
  WORKS_AT: "#60a5fa",
  REPORTS_TO: "#f472b6",
  PARTICIPATES_IN: "#a78bfa",
  DISCUSSES: "#34d399",
  DECIDED: "#fbbf24",
  MENTIONS: "#94a3b8",
  SENT_TO: "#fb923c",
  REFERENCES_DOC: "#e879f9",
  SCHEDULED: "#2dd4bf",
};

function typeColor(t: string) {
  return TYPE_COLORS[t] ?? "#6b7280";
}

function formatDate(ts: string | null): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
    });
  } catch {
    return ts;
  }
}

export default function TimelinePanel({ entityId }: Props) {
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [includeHistorical, setIncludeHistorical] = useState(true);

  useEffect(() => {
    if (!entityId) { setEntries([]); return; }
    let cancelled = false;
    setLoading(true);
    api.entityTimeline(entityId, includeHistorical)
      .then((e) => { if (!cancelled) setEntries(e); })
      .catch(() => { if (!cancelled) setEntries([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [entityId, includeHistorical]);

  if (!entityId) {
    return (
      <div className="empty-state">
        <Clock size={32} />
        <p>Select an entity to view its claim timeline</p>
      </div>
    );
  }

  if (loading) {
    return <div className="loading"><span className="spinner" /> Loading timeline…</div>;
  }

  return (
    <div style={{ padding: "0 4px" }}>
      {/* Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <label style={{ fontSize: 11, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={includeHistorical}
            onChange={(e) => setIncludeHistorical(e.target.checked)}
            style={{ accentColor: "var(--accent)" }}
          />
          Show historical
        </label>
        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)" }}>
          {entries.length} claim{entries.length !== 1 ? "s" : ""}
        </span>
      </div>

      {entries.length === 0 ? (
        <div className="empty-state" style={{ minHeight: 120 }}>
          <p>No claims found</p>
        </div>
      ) : (
        <div className="timeline">
          {entries.map((entry, i) => (
            <div key={entry.claim_id} className="timeline-item">
              {/* Vertical connector */}
              <div className="timeline-spine">
                <div
                  className="timeline-dot"
                  style={{ background: typeColor(entry.claim_type) }}
                />
                {i < entries.length - 1 && <div className="timeline-line" />}
              </div>

              {/* Content */}
              <div className="timeline-content">
                {/* Date range */}
                <div className="timeline-date">
                  {entry.valid_from ? formatDate(entry.valid_from) : "Unknown start"}
                  {" → "}
                  {entry.is_current ? (
                    <span style={{ color: "var(--accent)", fontWeight: 600 }}>current</span>
                  ) : entry.valid_to ? (
                    formatDate(entry.valid_to)
                  ) : (
                    <span style={{ color: "var(--text-muted)" }}>superseded</span>
                  )}
                </div>

                {/* Claim */}
                <div className="timeline-claim">
                  <span
                    className="type-badge"
                    style={{
                      background: `${typeColor(entry.claim_type)}22`,
                      color: typeColor(entry.claim_type),
                      fontSize: 10,
                      padding: "1px 6px",
                    }}
                  >
                    {entry.claim_type}
                  </span>
                  <span style={{ color: "var(--text-primary)", fontSize: 12, marginLeft: 4 }}>
                    {entry.subject} → {entry.object}
                  </span>
                </div>

                {/* Meta row */}
                <div style={{ display: "flex", gap: 10, marginTop: 4, alignItems: "center" }}>
                  {/* Confidence bar */}
                  <div style={{ flex: 1, height: 3, background: "var(--border)", borderRadius: 2 }}>
                    <div
                      style={{
                        height: "100%",
                        borderRadius: 2,
                        width: `${Math.round(entry.confidence * 100)}%`,
                        background:
                          entry.confidence >= 0.8
                            ? "#22c55e"
                            : entry.confidence >= 0.5
                            ? "var(--accent)"
                            : "#f59e0b",
                      }}
                    />
                  </div>
                  <span style={{ fontSize: 10, color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                    {Math.round(entry.confidence * 100)}%
                  </span>
                  {entry.evidence_count > 0 && (
                    <span style={{ fontSize: 10, color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                      {entry.evidence_count} src
                    </span>
                  )}
                  {entry.is_current ? (
                    <CheckCircle size={11} color="#22c55e" />
                  ) : (
                    <Archive size={11} color="var(--text-muted)" />
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
