import { useEffect, useRef, useCallback } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { circular } from "graphology-layout";
import type { GraphData } from "../api";
import { typeColor } from "../colors";

// Canonical display labels for legend
const LEGEND_COLORS: [string, string][] = [
  ["Person", "#60a5fa"],
  ["Organization", "#f472b6"],
  ["Project", "#a78bfa"],
  ["Meeting", "#34d399"],
];

interface Props {
  data: GraphData | null;
  selectedEntity: string | null;
  onSelectEntity: (id: string | null) => void;
}

export default function GraphView({ data, selectedEntity, onSelectEntity }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);

  // Build / rebuild the graph when data changes
  useEffect(() => {
    if (!containerRef.current) return;
    // Destroy previous instance
    if (sigmaRef.current) {
      sigmaRef.current.kill();
      sigmaRef.current = null;
    }

    if (!data || data.nodes.length === 0) {
      graphRef.current = null;
      return;
    }

    const graph = new Graph({ multi: true });
    graphRef.current = graph;

    // Add nodes
    for (const node of data.nodes) {
      if (graph.hasNode(node.id)) continue;
      graph.addNode(node.id, {
        label: node.label,
        size: 12,
        color: typeColor(node.type),
        nodeType: node.type,
        x: 0,
        y: 0,
      });
    }

    // Add edges
    for (const edge of data.edges) {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
      try {
        graph.addEdgeWithKey(edge.id, edge.source, edge.target, {
          label: edge.label,
          size: Math.max(1, edge.confidence * 3),
          color: `rgba(79, 110, 247, ${0.2 + edge.confidence * 0.5})`,
          edgeType: edge.type,
        });
      } catch {
        // duplicate edge key
      }
    }

    // Layout
    circular.assign(graph);
    if (graph.order > 1) {
      forceAtlas2.assign(graph, {
        iterations: 100,
        settings: {
          gravity: 1,
          scalingRatio: 10,
          barnesHutOptimize: graph.order > 50,
        },
      });
    }

    // Adjust node sizes by degree
    graph.forEachNode((node) => {
      const deg = graph.degree(node);
      graph.setNodeAttribute(node, "size", 8 + Math.min(deg * 3, 24));
    });

    // Create sigma
    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelColor: { color: "#e8eaf0" },
      labelFont: '"Inter", system-ui, sans-serif',
      labelSize: 12,
      labelWeight: "500",
      defaultEdgeType: "arrow",
      edgeLabelFont: '"Inter", system-ui, sans-serif',
      edgeLabelSize: 10,
      stagePadding: 40,
    });

    sigmaRef.current = sigma;

    // Click handler
    sigma.on("clickNode", ({ node }) => {
      onSelectEntity(node);
    });
    sigma.on("clickStage", () => {
      onSelectEntity(null);
    });

    return () => {
      sigma.kill();
      sigmaRef.current = null;
    };
  }, [data, onSelectEntity]);

  // Highlight selected node
  useEffect(() => {
    const graph = graphRef.current;
    const sigma = sigmaRef.current;
    if (!graph || !sigma) return;

    // Node reducer for visual state
    sigma.setSetting("nodeReducer", (node, attrs) => {
      const res = { ...attrs };
      if (selectedEntity && node !== selectedEntity) {
        // Check if neighbor
        const isNeighbor = graph.neighbors(selectedEntity).includes(node);
        if (!isNeighbor) {
          res.color = "#2a2d3e";
          res.label = "";
        }
      }
      if (node === selectedEntity) {
        res.highlighted = true;
        res.size = (attrs.size as number) * 1.3;
      }
      return res;
    });

    sigma.setSetting("edgeReducer", (edge, attrs) => {
      const res = { ...attrs };
      if (selectedEntity) {
        const src = graph.source(edge);
        const tgt = graph.target(edge);
        if (src !== selectedEntity && tgt !== selectedEntity) {
          res.color = "rgba(42, 45, 62, 0.3)";
          res.label = "";
        }
      }
      return res;
    });

    sigma.refresh();
  }, [selectedEntity]);

  const handleReset = useCallback(() => {
    const sigma = sigmaRef.current;
    if (sigma) {
      const camera = sigma.getCamera();
      camera.animate({ x: 0.5, y: 0.5, ratio: 1 }, { duration: 300 });
    }
    onSelectEntity(null);
  }, [onSelectEntity]);

  return (
    <div className="graph-area">
      <div className="graph-container" ref={containerRef} />

      {(!data || data.nodes.length === 0) && (
        <div className="graph-empty">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="6" cy="6" r="3" /><circle cx="18" cy="6" r="3" />
            <circle cx="12" cy="18" r="3" /><line x1="8.5" y1="7.5" x2="10" y2="16" />
            <line x1="15.5" y1="7.5" x2="14" y2="16" /><line x1="9" y1="6" x2="15" y2="6" />
          </svg>
          <p>No graph data</p>
          <small>Select an entity or run a query</small>
        </div>
      )}

      {data && data.nodes.length > 0 && (
        <>
          <div className="graph-legend">
            {LEGEND_COLORS.map(([type, color]) => (
              <span key={type} className="legend-item">
                <span className="legend-dot" style={{ background: color }} />
                {type}
              </span>
            ))}
          </div>
          <button
            onClick={handleReset}
            style={{
              position: "absolute", top: 12, right: 12,
              padding: "5px 10px", fontSize: 11, fontWeight: 500,
              background: "var(--bg-secondary)", border: "1px solid var(--border)",
              borderRadius: "var(--radius-sm)", color: "var(--text-secondary)",
              cursor: "pointer", fontFamily: "var(--font)"
            }}
          >
            Reset View
          </button>
        </>
      )}
    </div>
  );
}
