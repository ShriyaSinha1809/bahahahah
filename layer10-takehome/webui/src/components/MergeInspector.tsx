import { useState, useEffect } from "react";
import { api, type EntityDetail, type MergeEvent } from "../api";
import { typeColor } from "../colors";
import { Combine, Tag, Settings2, GitMerge, RotateCcw } from "lucide-react";

interface Props {
  entityId: string | null;
}

function formatDate(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return ts;
  }
}

export default function MergeInspector({ entityId }: Props) {
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [merges, setMerges] = useState<MergeEvent[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!entityId) { setEntity(null); setMerges([]); return; }
    let cancelled = false;
    setLoading(true);
    Promise.all([
      api.entity(entityId),
      api.entityMerges(entityId),
    ])
      .then(([e, m]) => {
        if (!cancelled) {
          setEntity(e);
          setMerges(m);
        }
      })
      .catch(() => { if (!cancelled) { setEntity(null); setMerges([]); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [entityId]);

  if (!entityId) {
    return (
      <div className="empty-state">
        <Combine size={32} />
        <p>Select an entity to inspect merges and aliases</p>
      </div>
    );
  }

  if (loading) {
    return <div className="loading"><span className="spinner" /> Loading…</div>;
  }

  if (!entity) {
    return (
      <div className="empty-state">
        <p>Entity not found</p>
      </div>
    );
  }

  const props = entity.properties || {};
  const propEntries = Object.entries(props);
  const activeMerges = merges.filter((m) => !m.reversed_at);
  const reversedMerges = merges.filter((m) => m.reversed_at);

  return (
    <div>
      <div className="entity-header">
        <h3>{entity.canonical_name}</h3>
        <span className="type-badge" style={{ background: `${typeColor(entity.entity_type)}22`, color: typeColor(entity.entity_type) }}>{entity.entity_type}</span>
      </div>

      {/* ID */}
      <div className="merge-section">
        <h4>Entity ID</h4>
        <code className="mono" style={{ color: "var(--text-secondary)", fontSize: 11 }}>
          {entity.entity_id}
        </code>
      </div>

      {/* Aliases */}
      <div className="merge-section">
        <h4><Tag size={11} style={{ marginRight: 4 }} /> Aliases ({entity.aliases.length})</h4>
        {entity.aliases.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>No known aliases</span>
        ) : (
          <div className="alias-list">
            {entity.aliases.map((alias) => (
              <span key={alias} className="alias-chip">{alias}</span>
            ))}
          </div>
        )}
      </div>

      {/* Merge History */}
      <div className="merge-section">
        <h4><GitMerge size={11} style={{ marginRight: 4 }} /> Merge History ({merges.length})</h4>
        {merges.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>No merges recorded</span>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {activeMerges.length > 0 && (
              <>
                <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 1 }}>Active</span>
                {activeMerges.map((ev) => (
                  <MergeEventRow key={ev.event_id} event={ev} />
                ))}
              </>
            )}
            {reversedMerges.length > 0 && (
              <>
                <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 1, marginTop: 4 }}>Reversed</span>
                {reversedMerges.map((ev) => (
                  <MergeEventRow key={ev.event_id} event={ev} reversed />
                ))}
              </>
            )}
          </div>
        )}
      </div>

      {/* Properties */}
      <div className="merge-section">
        <h4><Settings2 size={11} style={{ marginRight: 4 }} /> Properties</h4>
        {propEntries.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>No properties</span>
        ) : (
          <table className="properties-table">
            <tbody>
              {propEntries.map(([key, val]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td style={{ color: "var(--text-primary)" }}>
                    {typeof val === "object" ? JSON.stringify(val) : String(val)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Claim Stats */}
      <div className="merge-section">
        <h4><Combine size={11} style={{ marginRight: 4 }} /> Claim Stats</h4>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <div>
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>Total Claims</span>
            <div style={{ fontWeight: 600, fontSize: 20, color: "var(--accent)" }}>
              {entity.claim_count}
            </div>
          </div>
          <div>
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>Merges</span>
            <div style={{ fontWeight: 600, fontSize: 20, color: activeMerges.length > 0 ? "var(--accent)" : "var(--text-muted)" }}>
              {merges.length}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function MergeEventRow({ event, reversed = false }: { event: MergeEvent; reversed?: boolean }) {
  return (
    <div
      style={{
        background: reversed ? "var(--surface-muted, #1e1e1e)" : "var(--surface, #1a1a1a)",
        border: `1px solid ${reversed ? "#333" : "var(--border, #2a2a2a)"}`,
        borderRadius: 6,
        padding: "8px 10px",
        fontSize: 12,
        opacity: reversed ? 0.6 : 1,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 4 }}>
        <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>
          {reversed && <RotateCcw size={10} style={{ marginRight: 4 }} />}
          {event.action_type}
        </span>
        <span style={{ color: "var(--text-muted)", fontSize: 11 }}>{formatDate(event.created_at)}</span>
      </div>
      <div style={{ color: "var(--text-secondary)", marginBottom: 2 }}>
        <strong>Reason:</strong> {event.reason}
      </div>
      {event.confidence != null && (
        <div style={{ color: "var(--text-muted)" }}>
          <strong>Confidence:</strong> {(event.confidence * 100).toFixed(0)}%
        </div>
      )}
      <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>
        {event.source_ids.length} source{event.source_ids.length !== 1 ? "s" : ""} → {event.target_id.slice(0, 8)}…
      </div>
      {reversed && event.reversed_reason && (
        <div style={{ color: "#f59e0b", marginTop: 4, fontSize: 11 }}>
          Reversed: {event.reversed_reason} ({formatDate(event.reversed_at)})
        </div>
      )}
    </div>
  );
}

interface Props {
  entityId: string | null;
}

export default function MergeInspector({ entityId }: Props) {
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!entityId) { setEntity(null); return; }
    let cancelled = false;
    setLoading(true);
    api.entity(entityId)
      .then((e) => { if (!cancelled) setEntity(e); })
      .catch(() => { if (!cancelled) setEntity(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [entityId]);

  if (!entityId) {
    return (
      <div className="empty-state">
        <Combine size={32} />
        <p>Select an entity to inspect merges and aliases</p>
      </div>
    );
  }

  if (loading) {
    return <div className="loading"><span className="spinner" /> Loading…</div>;
  }

  if (!entity) {
    return (
      <div className="empty-state">
        <p>Entity not found</p>
      </div>
    );
  }

  const props = entity.properties || {};
  const propEntries = Object.entries(props);

  return (
    <div>
      <div className="entity-header">
        <h3>{entity.canonical_name}</h3>
        <span className="type-badge" style={{ background: `${typeColor(entity.entity_type)}22`, color: typeColor(entity.entity_type) }}>{entity.entity_type}</span>
      </div>

      {/* ID */}
      <div className="merge-section">
        <h4>Entity ID</h4>
        <code className="mono" style={{ color: "var(--text-secondary)", fontSize: 11 }}>
          {entity.entity_id}
        </code>
      </div>

      {/* Aliases */}
      <div className="merge-section">
        <h4><Tag size={11} style={{ marginRight: 4 }} /> Aliases</h4>
        {entity.aliases.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>No known aliases</span>
        ) : (
          <div className="alias-list">
            {entity.aliases.map((alias) => (
              <span key={alias} className="alias-chip">{alias}</span>
            ))}
          </div>
        )}
      </div>

      {/* Properties */}
      <div className="merge-section">
        <h4><Settings2 size={11} style={{ marginRight: 4 }} /> Properties</h4>
        {propEntries.length === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>No properties</span>
        ) : (
          <table className="properties-table">
            <tbody>
              {propEntries.map(([key, val]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td style={{ color: "var(--text-primary)" }}>
                    {typeof val === "object" ? JSON.stringify(val) : String(val)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Claim Stats */}
      <div className="merge-section">
        <h4><Combine size={11} style={{ marginRight: 4 }} /> Claim Stats</h4>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <div>
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>Total Claims</span>
            <div style={{ fontWeight: 600, fontSize: 20, color: "var(--accent)" }}>
              {entity.claim_count}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
