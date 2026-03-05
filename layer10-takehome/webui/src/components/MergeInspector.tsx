import { useState, useEffect } from "react";
import { api, type EntityDetail } from "../api";
import { typeColor } from "../colors";
import { Combine, Tag, Settings2 } from "lucide-react";

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
