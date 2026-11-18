// The state map: lanes of boxes, arrows for the moves (DESIGN.md 3.8).
// Takes the chain as props so the race view and the states view can share it.
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { familyColor, StateGlyph } from "./StateGlyph";
import { pct, usd } from "./stateFormat";
import {
  layoutStates,
  moveSelection,
  nodeWords,
  wordsSentence,
  type LanesBy,
  type LevelDef,
} from "./stateMap";
import type { StateEdge, StateNode } from "./types";
import "./stateMap.css";

export type StateChain = {
  nodes: StateNode[];
  edges: StateEdge[];
  coverage?: number;
};

export type StateMapProps = {
  chain: StateChain;
  selected: string | null;
  onSelect: (id: string) => void;
  /** Money made on a normal day, for the box edge colour. */
  avgPerDay: number;
  levels?: readonly (LevelDef | string)[] | null;
  families?: readonly string[];
  lanesBy?: LanesBy;
};

const KEYS = new Set([
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
]);
const CHAR_W = 6.6;

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function stripeColor(perDay: number, avgPerDay: number): string {
  if (perDay < 0) return "var(--tf-loss)";
  return perDay >= avgPerDay ? "var(--tf-gain-fill)" : "var(--tf-today)";
}

function clip(text: string, room: number): string {
  const max = Math.max(4, Math.floor(room / CHAR_W));
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Scrolls a state box into view and focuses it (used by the money leaks list). */
export function revealState(root: ParentNode | null, id: string): void {
  const el = root?.querySelector<SVGGElement>(
    `[data-state-id="${CSS.escape(id)}"]`,
  );
  if (!el) return;
  const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
  el.focus({ preventScroll: true });
}

export function StateMap({
  chain,
  selected,
  onSelect,
  avgPerDay,
  levels,
  families,
  lanesBy = "auto",
}: StateMapProps) {
  const [wrap, width] = useWidth<HTMLDivElement>();
  const layout = useMemo(
    () =>
      layoutStates(chain.nodes, chain.edges, lanesBy, {
        width: width || 900,
        order: families,
        levels,
      }),
    [chain, lanesBy, width, families, levels],
  );
  const refs = useRef(new Map<string, SVGGElement>());
  const byId = useMemo(
    () => new Map(chain.nodes.map((n) => [n.id, n])),
    [chain],
  );

  const onKey = (id: string) => (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(id);
      return;
    }
    if (!KEYS.has(e.key)) return;
    e.preventDefault();
    const next = moveSelection(layout, id, e.key);
    onSelect(next);
    refs.current.get(next)?.focus();
  };

  const label =
    layout.mode === "setup"
      ? "State map. Columns are what the press is set to. Select a box to see where the plant goes next."
      : layout.mode === "level"
        ? "State map. Columns are the stock level of the first family. Select a box to see where the plant goes next."
        : "State map. Boxes are sorted by share of days. Select a box to see where the plant goes next.";

  const edges = layout.edges.map((e) => ({
    ...e,
    kind: e.from === selected ? "out" : e.to === selected ? "in" : "hide",
  }));
  // Draw the faint ones first so the lit arrows sit on top.
  const rank = { hide: 0, in: 1, out: 2 } as const;
  edges.sort(
    (a, b) =>
      rank[a.kind as keyof typeof rank] - rank[b.kind as keyof typeof rank],
  );

  return (
    <div className="tf-sm-scroll" ref={wrap}>
      {width > 0 && (
        <svg
          className="tf-sm-svg"
          width={layout.width}
          height={layout.height}
          role="group"
          aria-label={label}
          data-state-map=""
          data-mode={layout.mode}
        >
          <defs>
            <marker
              id="tf-sm-head"
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M0 0 L8 4 L0 8 z" fill="var(--ba-accent)" />
            </marker>
          </defs>
          {layout.lanes.map((lane) => (
            <g key={lane.key} className="tf-sm-lane">
              <rect
                x={lane.x + 6}
                y={0}
                width={lane.w - 12}
                height={layout.height}
                rx={3}
                fill="var(--ba-surface-2)"
              />
              {layout.mode === "setup" && (
                <rect
                  x={lane.x + 18}
                  y={13}
                  width={10}
                  height={10}
                  rx={2}
                  fill={familyColor(lane.key, families)}
                  stroke="var(--tf-axis)"
                  strokeWidth={0.5}
                />
              )}
              <text
                x={lane.x + (layout.mode === "setup" ? 34 : 18)}
                y={22}
                className="tf-sm-lane-title"
              >
                {lane.title}
              </text>
            </g>
          ))}
          <g className="tf-sm-edges" aria-hidden="true">
            {edges.map((e) => (
              <path
                key={`${e.from}>${e.to}`}
                d={e.d}
                className={`tf-sm-edge ${e.kind}`}
                strokeWidth={1 + e.prob * 7}
                markerEnd={e.kind === "out" ? "url(#tf-sm-head)" : undefined}
              />
            ))}
          </g>
          {layout.order.map((id) => {
            const n = byId.get(id)!;
            const b = layout.boxes[id];
            const w = nodeWords(n, levels);
            const sel = id === selected;
            const glyphFams = n.parts_of_label
              ? Object.keys(n.parts_of_label.levels).length
              : 0;
            const textX = glyphFams ? 20 + glyphFams * 9 + 6 : 12;
            const meta = `${pct(n.share)} · ${usd(n.per_day)} a day`;
            return (
              <g
                key={id}
                ref={(el) => {
                  if (el) refs.current.set(id, el);
                  else refs.current.delete(id);
                }}
                className={`tf-sm-node${sel ? " sel" : ""}`}
                transform={`translate(${b.x},${b.y})`}
                role="button"
                tabIndex={0}
                aria-pressed={sel}
                aria-label={`${wordsSentence(w)} ${pct(n.share)} of days. ${usd(n.per_day)} a day.`}
                data-state-id={id}
                onClick={() => onSelect(id)}
                onKeyDown={onKey(id)}
              >
                <title>{wordsSentence(w)}</title>
                <rect className="tf-sm-box" width={b.w} height={b.h} rx={3} />
                <rect
                  className="tf-sm-stripe"
                  width={4}
                  height={b.h}
                  rx={2}
                  fill={stripeColor(n.per_day, avgPerDay)}
                />
                {n.parts_of_label && (
                  <StateGlyph
                    levels={n.parts_of_label.levels}
                    order={families}
                    levelDefs={levels}
                    x={14}
                    y={11}
                  />
                )}
                <text x={textX} y={20} className="tf-sm-name">
                  {clip(w.title, b.w - textX - 8)}
                </text>
                <text
                  x={textX}
                  y={37}
                  className={`tf-sm-meta${n.per_day < 0 ? " loss" : ""}`}
                >
                  {meta}
                </text>
              </g>
            );
          })}
          <g className="tf-sm-labels" aria-hidden="true">
            {edges
              .filter((e) => e.kind === "out")
              .map((e) => (
                <g key={`l${e.to}`}>
                  <rect
                    x={
                      e.label.anchor === "start"
                        ? e.label.x - 3
                        : e.label.x - 31
                    }
                    y={e.label.y - 11}
                    width={34}
                    height={15}
                    rx={2}
                    fill="var(--ba-surface)"
                  />
                  <text
                    x={e.label.x}
                    y={e.label.y}
                    textAnchor={e.label.anchor}
                    className="tf-sm-edge-label"
                  >
                    {pct(e.prob, 0)}
                  </text>
                </g>
              ))}
          </g>
        </svg>
      )}
    </div>
  );
}
