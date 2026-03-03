import { useState, useEffect } from "react";
import { api, type ClaimWithEvidence, type EntityDetail } from "../api";
import { FileText, Clock, User, Mail, ChevronDown, ChevronRight, Shield } from "lucide-react";

interface Props {
  entityId: string | null;
}

function confidenceClass(c: number): string {
  if (c >= 0.8) return "confidence-high";
  if (c >= 0.5) return "confidence-medium";
  return "confidence-low";
}

function formatDate(d: string | null): string {
  if (!d) return "—";
  try {
    return new Date(d).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch {
    return d;
  }
}

export default function EvidencePanel({ entityId }: Props) {
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [claims, setClaims] = useState<ClaimWithEvidence[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!entityId) {
      setEntity(null);
      setClaims([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    Promise.all([api.entity(entityId), api.entityClaims(entityId)])
      .then(([ent, cls]) => {
        if (cancelled) return;
        setEntity(ent);
        setClaims(cls);
        setExpanded(new Set());
      })
      .catch(() => {
        if (!cancelled) {
          setEntity(null);
          setClaims([]);
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [entityId]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  if (!entityId) {
    return (
      <div className="empty-state">
        <FileText size={32} />
        <p>Select an entity to view claims and evidence</p>
      </div>
    );
  }

  if (loading) {
    return <div className="loading"><span className="spinner" /> Loading…</div>;
  }

  return (
    <div>
      {entity && (
        <div className="entity-header">
          <h3>{entity.canonical_name}</h3>
          <span className={`type-badge ${entity.entity_type}`}>{entity.entity_type}</span>
        </div>
      )}

      {claims.length === 0 ? (
        <div className="empty-state" style={{ padding: "20px 0" }}>
          <Shield size={24} />
          <p>No claims found</p>
        </div>
      ) : (
        claims.map((claim) => {
          const isExpanded = expanded.has(claim.claim_id);
          return (
            <div
              key={claim.claim_id}
              className={`claim-card ${isExpanded ? "expanded" : ""}`}
              onClick={() => toggle(claim.claim_id)}
            >
              <div className="claim-top">
                <span className="claim-type">
                  {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  {" "}{claim.claim_type}
                </span>
                <span className={`claim-confidence ${confidenceClass(claim.confidence)}`}>
                  {Math.round(claim.confidence * 100)}%
                </span>
              </div>
              <div className="claim-desc">
                <strong>{claim.subject}</strong> → <strong>{claim.object}</strong>
                {!claim.is_current && (
                  <span style={{ marginLeft: 6, color: "var(--text-muted)", fontSize: 10 }}>(historical)</span>
                )}
              </div>
              {(claim.valid_from || claim.valid_to) && (
                <div className="claim-time">
                  <Clock size={10} style={{ marginRight: 3 }} />
                  {formatDate(claim.valid_from)} – {formatDate(claim.valid_to)}
                </div>
              )}

              {/* Evidence */}
              {isExpanded && claim.evidence.length > 0 && (
                <div className="evidence-list">
                  {claim.evidence.map((ev, i) => (
                    <div key={`${ev.source_id}-${i}`} className="evidence-item">
                      <div className="evidence-excerpt">"{ev.excerpt}"</div>
                      <div className="evidence-meta">
                        {ev.sender && (
                          <span><User size={10} /> {ev.sender}</span>
                        )}
                        {ev.subject && (
                          <span><Mail size={10} /> {ev.subject}</span>
                        )}
                        {ev.source_date && (
                          <span><Clock size={10} /> {formatDate(ev.source_date)}</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {isExpanded && claim.evidence.length === 0 && (
                <div className="evidence-list" style={{ color: "var(--text-muted)", fontSize: 11 }}>
                  No evidence excerpts available
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
