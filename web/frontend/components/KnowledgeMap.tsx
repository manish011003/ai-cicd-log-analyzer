"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useTheme } from "next-themes";
import { fetchKnowledgeGraph } from "@/lib/api";
import type {
  KnowledgeEdge,
  KnowledgeGraphResponse,
  KnowledgeNode,
  KnowledgeNodeKind,
} from "@/lib/types";
import { Button } from "./ui/button";

// react-force-graph-2d is a client-only canvas component; importing it on the
// server breaks the Next.js build because it touches `window`.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

const NODE_COLORS: Record<KnowledgeNodeKind, string> = {
  solution: "#6366f1",
  error_class: "#f97316",
  job: "#10b981",
  stage: "#06b6d4",
};

const KIND_LABELS: Record<KnowledgeNodeKind, string> = {
  solution: "Solution",
  error_class: "Error class",
  job: "Job",
  stage: "Stage",
};

type GraphNodeView = KnowledgeNode & {
  // Dynamic position fields written by react-force-graph during simulation.
  x?: number;
  y?: number;
};

type GraphEdgeView = Omit<KnowledgeEdge, "source" | "target"> & {
  source: string | GraphNodeView;
  target: string | GraphNodeView;
};

interface FilterState {
  kinds: Set<KnowledgeNodeKind>;
  edgeKinds: Set<KnowledgeEdge["kind"]>;
}

const ALL_KINDS: KnowledgeNodeKind[] = ["solution", "error_class", "job", "stage"];
const ALL_EDGE_KINDS: KnowledgeEdge["kind"][] = ["similar", "resolves", "occurs_in", "in_job"];

function defaultFilter(): FilterState {
  return {
    kinds: new Set(ALL_KINDS),
    edgeKinds: new Set(ALL_EDGE_KINDS),
  };
}

export default function KnowledgeMap() {
  const [data, setData] = useState<KnowledgeGraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterState>(() => defaultFilter());
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 800, h: 560 });
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetchKnowledgeGraph({ refresh });
      setData(resp);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load knowledge graph");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Resize the canvas when the side panel toggles or the window changes.
  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const cr = entry.contentRect;
        setSize({ w: Math.max(320, Math.floor(cr.width)), h: Math.max(360, Math.floor(cr.height)) });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const filtered = useMemo(() => {
    if (!data) return { nodes: [] as GraphNodeView[], links: [] as GraphEdgeView[] };
    const keep = new Set(
      data.nodes.filter((n) => filter.kinds.has(n.kind)).map((n) => n.id),
    );
    const nodes: GraphNodeView[] = data.nodes
      .filter((n) => keep.has(n.id))
      .map((n) => ({ ...n }));
    const links: GraphEdgeView[] = data.edges
      .filter(
        (e) => filter.edgeKinds.has(e.kind) && keep.has(e.source) && keep.has(e.target),
      )
      .map((e) => ({ ...e }));
    return { nodes, links };
  }, [data, filter]);

  const selectedNode = useMemo(() => {
    if (!data || !selectedId) return null;
    return data.nodes.find((n) => n.id === selectedId) ?? null;
  }, [data, selectedId]);

  const selectedNeighbours = useMemo(() => {
    if (!data || !selectedId) return [] as { node: KnowledgeNode; weight?: number }[];
    const out: { node: KnowledgeNode; weight?: number }[] = [];
    const byId = new Map(data.nodes.map((n) => [n.id, n] as const));
    for (const e of data.edges) {
      if (e.source === selectedId && byId.has(e.target)) {
        out.push({ node: byId.get(e.target)!, weight: e.weight });
      } else if (e.target === selectedId && byId.has(e.source)) {
        out.push({ node: byId.get(e.source)!, weight: e.weight });
      }
    }
    out.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
    return out.slice(0, 25);
  }, [data, selectedId]);

  const stats = data?.stats;

  // Node size scales with degree so god nodes pop visually.
  const nodeRadius = useCallback((node: GraphNodeView) => {
    if (node.kind !== "solution") return 5;
    const d = node.degree ?? 0;
    return Math.min(14, 5 + Math.sqrt(d) * 1.6);
  }, []);

  const nodePaint = useCallback(
    (node: GraphNodeView, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const r = nodeRadius(node);
      const isSelected = node.id === selectedId;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, r, 0, 2 * Math.PI, false);
      ctx.fillStyle = NODE_COLORS[node.kind];
      ctx.globalAlpha = isSelected ? 1 : 0.92;
      ctx.fill();
      if (isSelected) {
        ctx.lineWidth = 2 / globalScale;
        ctx.strokeStyle = isDark ? "#f8fafc" : "#0f172a";
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      // Only draw labels above a zoom threshold to keep the canvas readable
      // when the whole graph is in view.
      if (globalScale >= 1.4 || isSelected) {
        const label = node.label || node.id;
        const fontSize = Math.max(9, 11 / globalScale);
        ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const x = (node.x ?? 0) + r + 4;
        const y = node.y ?? 0;
        const text = label.length > 36 ? `${label.slice(0, 35)}…` : label;
        // Halo so the label stays legible over node fills and either bg.
        ctx.lineWidth = Math.max(2, 3 / globalScale);
        ctx.strokeStyle = isDark ? "rgba(2, 6, 23, 0.85)" : "rgba(255, 255, 255, 0.85)";
        ctx.lineJoin = "round";
        ctx.miterLimit = 2;
        ctx.strokeText(text, x, y);
        ctx.fillStyle = isDark ? "rgba(226, 232, 240, 0.95)" : "rgba(15, 23, 42, 0.85)";
        ctx.fillText(text, x, y);
      }
    },
    [nodeRadius, selectedId, isDark],
  );

  const edgeColor = useCallback(
    (edge: GraphEdgeView) => {
      if (edge.kind === "similar") {
        const w = edge.weight ?? 0.5;
        const alpha = Math.min(0.85, 0.25 + w * 0.6);
        return `rgba(99, 102, 241, ${alpha})`;
      }
      if (edge.kind === "resolves") return "rgba(249, 115, 22, 0.55)";
      if (edge.kind === "occurs_in") return "rgba(6, 182, 212, 0.45)";
      return "rgba(16, 185, 129, 0.45)";
    },
    [],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <KnowledgeMetrics stats={stats} loading={loading} onRefresh={() => void load(true)} />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-h-[420px] flex-col rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/70">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
            <div>
              <h3 className="text-sm font-semibold">Knowledge Map</h3>
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                {data
                  ? `${data.stats.total_solutions} solutions · ${data.stats.total_similarity_edges} similarity links · ${data.stats.communities} communities`
                  : "Loading the accepted-solutions corpus…"}
              </p>
            </div>
            <FilterChips filter={filter} setFilter={setFilter} />
          </div>
          <div ref={containerRef} className="relative min-h-[400px] flex-1 overflow-hidden">
            {loading && (
              <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 dark:bg-slate-950/60">
                <div className="text-xs text-slate-600 dark:text-slate-300">Building graph…</div>
              </div>
            )}
            {error && (
              <div className="absolute inset-0 z-10 flex items-center justify-center p-6">
                <div className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                  <span className="mr-1 font-semibold">Error:</span>
                  {error}
                </div>
              </div>
            )}
            {!loading && !error && data && data.stats.total_solutions === 0 && (
              <div className="absolute inset-0 z-10 flex items-center justify-center p-6">
                <div className="max-w-md rounded-lg border border-slate-300 bg-slate-50 px-4 py-4 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
                  No accepted solutions yet. Once an operator accepts a fix in the
                  Failure Card, it shows up here as a node and is wired into the
                  similarity map.
                </div>
              </div>
            )}
            {!loading && !error && data && data.stats.total_solutions > 0 && (
              <ForceGraph2D
                graphData={filtered}
                width={size.w}
                height={size.h}
                cooldownTicks={120}
                nodeRelSize={5}
                linkColor={(link: object) => edgeColor(link as GraphEdgeView)}
                linkWidth={(link: object) => {
                  const e = link as GraphEdgeView;
                  if (e.kind !== "similar") return 1;
                  return 0.5 + (e.weight ?? 0.5) * 2;
                }}
                onNodeClick={(node: object) => {
                  const n = node as GraphNodeView;
                  setSelectedId(n.id);
                }}
                onBackgroundClick={() => setSelectedId(null)}
                nodeCanvasObject={(node: object, ctx: CanvasRenderingContext2D, scale: number) =>
                  nodePaint(node as GraphNodeView, ctx, scale)
                }
                nodePointerAreaPaint={(node: object, color: string, ctx: CanvasRenderingContext2D) => {
                  const n = node as GraphNodeView;
                  ctx.fillStyle = color;
                  ctx.beginPath();
                  ctx.arc(n.x ?? 0, n.y ?? 0, nodeRadius(n) + 2, 0, 2 * Math.PI, false);
                  ctx.fill();
                }}
              />
            )}
          </div>
        </div>

        <SidePanel
          node={selectedNode}
          neighbours={selectedNeighbours}
          stats={stats}
          onClear={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}

function KnowledgeMetrics({
  stats,
  loading,
  onRefresh,
}: {
  stats: KnowledgeGraphResponse["stats"] | undefined;
  loading: boolean;
  onRefresh: () => void;
}) {
  const coverage = stats?.kb_coverage != null ? Math.round(stats.kb_coverage * 100) : null;
  const reuseRate =
    stats && stats.total_sessions
      ? Math.round(((stats.matched_sessions ?? 0) / stats.total_sessions) * 100)
      : null;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Metric label="Solutions in KB" value={stats?.total_solutions ?? 0} />
        <Metric
          label="Communities"
          value={stats?.communities ?? 0}
          hint={
            stats?.largest_community_size
              ? `largest ${stats.largest_community_size}`
              : undefined
          }
        />
        <Metric
          label="Similarity links"
          value={stats?.total_similarity_edges ?? 0}
          hint={
            stats?.avg_neighbours != null
              ? `avg ${stats.avg_neighbours.toFixed(1)}/node`
              : undefined
          }
        />
        <Metric
          label="Isolated"
          value={stats?.isolated_solutions ?? 0}
          tone="text-amber-700 dark:text-amber-300"
          hint="one-offs / gaps"
        />
        <Metric
          label="KB coverage"
          value={coverage != null ? `${coverage}%` : "—"}
          hint={
            stats?.accepted_sessions != null && stats?.total_solutions != null
              ? `${stats.total_solutions} / ${stats.accepted_sessions}`
              : undefined
          }
        />
        <Metric
          label="Reuse rate"
          value={reuseRate != null ? `${reuseRate}%` : "—"}
          hint={
            stats?.matched_sessions != null && stats?.total_sessions != null
              ? `${stats.matched_sessions} / ${stats.total_sessions}`
              : undefined
          }
          tone="text-emerald-700 dark:text-emerald-300"
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900/60">
        <div className="flex flex-wrap items-center gap-2">
          {(stats?.god_nodes ?? []).slice(0, 3).map((g) => (
            <span
              key={g.id}
              className="rounded-md border border-indigo-300 bg-indigo-50 px-2 py-0.5 text-[11px] text-indigo-800 dark:border-indigo-800/60 dark:bg-indigo-950/40 dark:text-indigo-200"
              title={g.label}
            >
              <span className="font-semibold">★ {g.error_class}</span>
              <span className="ml-1 tabular-nums">·{g.degree} links</span>
            </span>
          ))}
          {(stats?.top_error_classes ?? []).slice(0, 4).map((ec) => (
            <span
              key={ec.error_class}
              className="rounded-md border border-orange-300 bg-orange-50 px-2 py-0.5 text-[11px] text-orange-800 dark:border-orange-800/60 dark:bg-orange-950/40 dark:text-orange-200"
            >
              {ec.error_class}
              <span className="ml-1 tabular-nums">·{ec.count}</span>
            </span>
          ))}
        </div>
        <Button size="sm" variant="outline" onClick={onRefresh} disabled={loading}>
          {loading ? "Refreshing…" : "Rebuild graph"}
        </Button>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  tone = "text-slate-900 dark:text-slate-100",
}: {
  label: string;
  value: number | string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 dark:border-slate-800 dark:bg-slate-900/60">
      <p className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
      <p className={`mt-0.5 text-xl font-semibold tabular-nums leading-tight ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[10px] text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  );
}

function FilterChips({
  filter,
  setFilter,
}: {
  filter: FilterState;
  setFilter: (next: FilterState) => void;
}) {
  function toggleKind(kind: KnowledgeNodeKind) {
    const next = new Set(filter.kinds);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    setFilter({ ...filter, kinds: next });
  }
  function toggleEdge(kind: KnowledgeEdge["kind"]) {
    const next = new Set(filter.edgeKinds);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    setFilter({ ...filter, edgeKinds: next });
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {ALL_KINDS.map((k) => (
        <button
          key={`node-${k}`}
          type="button"
          onClick={() => toggleKind(k)}
          className={`rounded-md border px-2 py-0.5 text-[11px] transition ${
            filter.kinds.has(k)
              ? "border-slate-300 bg-slate-100 text-slate-800 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              : "border-slate-200 bg-white text-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-500"
          }`}
          title={`${KIND_LABELS[k]} nodes`}
        >
          <span
            className="mr-1 inline-block h-2 w-2 rounded-full align-middle"
            style={{ backgroundColor: NODE_COLORS[k] }}
          />
          {KIND_LABELS[k]}
        </button>
      ))}
      <span className="mx-1 hidden text-slate-300 sm:inline dark:text-slate-700">|</span>
      {ALL_EDGE_KINDS.map((k) => (
        <button
          key={`edge-${k}`}
          type="button"
          onClick={() => toggleEdge(k)}
          className={`rounded-md border px-2 py-0.5 text-[10px] transition ${
            filter.edgeKinds.has(k)
              ? "border-slate-300 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
              : "border-slate-200 bg-white text-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-500"
          }`}
          title={`Show ${k} edges`}
        >
          {k}
        </button>
      ))}
    </div>
  );
}

function SidePanel({
  node,
  neighbours,
  stats,
  onClear,
}: {
  node: KnowledgeNode | null;
  neighbours: { node: KnowledgeNode; weight?: number }[];
  stats: KnowledgeGraphResponse["stats"] | undefined;
  onClear: () => void;
}) {
  if (!node) {
    return (
      <aside className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/70">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          Inspector
        </h3>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Click a node in the map to inspect the underlying solution, its error
          class, and the other nodes most similar to it.
        </p>
        {stats?.god_nodes?.length ? (
          <div>
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Most reused solutions
            </p>
            <ul className="space-y-1.5">
              {stats.god_nodes.slice(0, 5).map((g) => (
                <li
                  key={g.id}
                  className="rounded-md border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] dark:border-slate-800 dark:bg-slate-950/60"
                >
                  <p className="truncate font-medium text-slate-800 dark:text-slate-200" title={g.label}>
                    {g.label}
                  </p>
                  <p className="text-slate-500 dark:text-slate-400">
                    {g.error_class} · {g.degree} similar
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </aside>
    );
  }

  const isSolution = node.kind === "solution";

  return (
    <aside className="flex flex-col gap-3 overflow-y-auto rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/70">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            {KIND_LABELS[node.kind]}
          </p>
          <p
            className="mt-1 break-words text-sm font-semibold text-slate-900 dark:text-slate-100"
            title={node.label}
          >
            {node.label}
          </p>
        </div>
        <button
          type="button"
          onClick={onClear}
          className="shrink-0 rounded-md border border-slate-300 px-2 py-0.5 text-[10px] text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          close
        </button>
      </div>

      {isSolution && (
        <>
          <dl className="grid grid-cols-2 gap-2 text-[11px]">
            <Detail label="Error class" value={node.error_class || "—"} />
            <Detail label="Reuse degree" value={String(node.degree ?? 0)} />
            <Detail label="Job" value={node.job_name || "—"} />
            <Detail label="Stage" value={node.stage_name || "—"} />
            <Detail label="Build" value={node.build_number ? `#${node.build_number}` : "—"} />
            <Detail
              label="PG sessions"
              value={
                node.pg_session_count != null ? String(node.pg_session_count) : "—"
              }
              hint="sessions sharing this fingerprint"
            />
            <Detail label="Community" value={String(node.community_id ?? 0)} />
            <Detail
              label="Created"
              value={node.created_at ? new Date(node.created_at).toLocaleDateString() : "—"}
            />
          </dl>

          {node.fingerprint_text && (
            <div>
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Fingerprint
              </p>
              <pre className="max-h-32 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 text-[10.5px] leading-snug text-slate-700 dark:border-slate-800 dark:bg-slate-950/70 dark:text-slate-300">
                {node.fingerprint_text}
              </pre>
            </div>
          )}

          {node.solution && (
            <div>
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Stored solution
              </p>
              <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-emerald-200 bg-emerald-50/70 p-2 text-[11px] leading-snug text-slate-800 dark:border-emerald-900/50 dark:bg-emerald-950/30 dark:text-emerald-100">
                {node.solution}
              </pre>
            </div>
          )}
        </>
      )}

      {node.kind === "stage" && node.job_name && (
        <Detail label="Belongs to job" value={node.job_name} />
      )}

      {neighbours.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Connected nodes
          </p>
          <ul className="space-y-1">
            {neighbours.map((n) => (
              <li
                key={`${n.node.id}-${n.weight ?? "x"}`}
                className="flex items-center justify-between gap-2 rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] dark:border-slate-800 dark:bg-slate-950/60"
              >
                <span className="flex items-center gap-1.5 truncate" title={n.node.label}>
                  <span
                    className="inline-block h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: NODE_COLORS[n.node.kind] }}
                  />
                  <span className="truncate text-slate-800 dark:text-slate-200">{n.node.label}</span>
                </span>
                {n.weight != null && (
                  <span className="shrink-0 tabular-nums text-slate-500 dark:text-slate-400">
                    {n.weight.toFixed(2)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  );
}

function Detail({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="truncate text-[11.5px] font-medium text-slate-800 dark:text-slate-200" title={value}>
        {value}
      </dd>
      {hint && <p className="text-[10px] text-slate-500 dark:text-slate-500">{hint}</p>}
    </div>
  );
}
