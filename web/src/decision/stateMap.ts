// State map: pure layout and words (DESIGN.md 3.8). No DOM, no React.
// Layout is computed, fixed and repeatable. No force layout.
import type { StateEdge, StateNode } from "./types";

export type PartsOfLabel = StateNode["parts_of_label"];
export type LevelDef = { name: string; below?: number | null };
export type LanesBy = "auto" | "setup" | "level" | "grid";
export type StateWords = { title: string; setup: string };

const DEFAULT_LEVELS = ["out", "low", "ok", "high"];
const NORMAL = new Set(["ok", "normal"]);

function levelNames(levels?: readonly (LevelDef | string)[] | null): string[] {
  if (!levels || !levels.length) return DEFAULT_LEVELS;
  return levels.map((l) => (typeof l === "string" ? l : l.name));
}

function cap(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function joinAnd(items: string[]): string {
  if (items.length < 2) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function levelWord(name: string): string {
  return name === "ok" ? "OK" : name;
}

/** Where a level sits in the ordered list. The lowest level counts as low (orange). */
export function levelInfo(
  level: string,
  levels?: readonly (LevelDef | string)[] | null,
): { index: number; count: number; low: boolean } {
  const names = levelNames(levels);
  const index = names.indexOf(level);
  return {
    index,
    count: names.length,
    low: index === 0 || level === "low" || level === "out",
  };
}

function isNormal(level: string, names: string[]): boolean {
  if (names.some((n) => NORMAL.has(n))) return NORMAL.has(level);
  return names.length >= 3 && names.length % 2 === 1
    ? names.indexOf(level) === (names.length - 1) / 2
    : false;
}

/** Plain words for a state: "Grey high, natural low" and "Press on black". */
export function stateWords(
  parts: PartsOfLabel,
  levels?: readonly (LevelDef | string)[] | null,
): StateWords {
  if (!parts) return { title: "", setup: "" };
  const names = levelNames(levels);
  const setup = parts.setup ? `Press on ${parts.setup}` : "";
  const entries = Object.entries(parts.levels);
  if (!entries.length) return { title: "", setup };
  const distinct = new Set(entries.map(([, v]) => v));
  if (distinct.size === 1)
    return { title: `All stock ${levelWord(entries[0][1])}`, setup };
  const rank = (v: string) => {
    const i = names.indexOf(v);
    return i < 0 ? -1 : i;
  };
  const groups = [...distinct]
    .filter((v) => !isNormal(v, names))
    .sort((a, b) => rank(b) - rank(a))
    .map((v) => {
      const fams = entries.filter(([, l]) => l === v).map(([f]) => f);
      return `${joinAnd(fams)} ${levelWord(v)}`;
    });
  return { title: cap(groups.join(", ")), setup };
}

/** Reads "grey:ok black:high | natural" back into levels and setup. Null if it is not that shape. */
export function partsFromLabel(label: string): PartsOfLabel {
  const [stock, setup] = label.split(" | ");
  const pairs = stock.trim().split(/\s+/).filter(Boolean);
  if (!pairs.length || !pairs.every((p) => /^[^:]+:[^:]+$/.test(p)))
    return null;
  return {
    levels: Object.fromEntries(pairs.map((p) => p.split(":"))),
    setup: setup?.trim() || null,
  };
}

/** Words for a node: from its parts, else the words Python sent, else the raw label. */
export function nodeWords(
  node: StateNode,
  levels?: readonly (LevelDef | string)[] | null,
): StateWords {
  if (node.parts_of_label) {
    const w = stateWords(node.parts_of_label, levels);
    if (w.title) return w;
  }
  if (node.words) return node.words;
  return { title: node.label, setup: "" };
}

/** A full sentence: "All stock OK. Press on grey." */
export function wordsSentence(w: StateWords): string {
  return [w.title, w.setup]
    .filter(Boolean)
    .map((t) => `${t}.`)
    .join(" ");
}

// ---------- layout ----------

export type Box = {
  x: number;
  y: number;
  w: number;
  h: number;
  lane: number;
  row: number;
};
export type Lane = {
  key: string;
  title: string;
  x: number;
  w: number;
  ids: string[];
};
export type EdgePath = {
  from: string;
  to: string;
  prob: number;
  d: string;
  label: { x: number; y: number; anchor: "start" | "end" };
};
export type StateLayout = {
  mode: "setup" | "level" | "grid";
  width: number;
  height: number;
  lanes: Lane[];
  /** Ids in reading order (grid mode) or lane by lane (lane modes). */
  order: string[];
  /** Grid columns (grid mode). Lane count otherwise. */
  columns: number;
  boxes: Record<string, Box>;
  edges: EdgePath[];
};

export type LayoutOptions = {
  width?: number;
  /** Preferred lane order, for example the families in capsule order. */
  order?: readonly string[];
  levels?: readonly (LevelDef | string)[] | null;
};

const BOX_H = 48;
const GAP = 12;
const LANE_TOP = 34;
const GRID_TOP = 8;
const MIN_LANE = 170;
const MAX_BOX = 232;
const GRID_BOX = 220;
const PAD = 6;

const r1 = (v: number) => Math.round(v * 10) / 10;

function byShare(a: StateNode, b: StateNode): number {
  return b.share - a.share || a.id.localeCompare(b.id);
}

function laneKey(n: StateNode, mode: "setup" | "level"): string | null {
  const p = n.parts_of_label;
  if (!p) return null;
  if (mode === "setup") return p.setup ?? null;
  const first = Object.entries(p.levels)[0];
  return first ? `${first[0]}:${first[1]}` : null;
}

function pickMode(nodes: StateNode[], lanesBy: LanesBy): StateLayout["mode"] {
  const has = (m: "setup" | "level") =>
    nodes.length > 0 && nodes.every((n) => laneKey(n, m) !== null);
  if (lanesBy === "grid") return "grid";
  if (lanesBy !== "level" && has("setup")) return "setup";
  if (has("level")) return "level";
  return "grid";
}

function laneOrder(
  keys: string[],
  shareOf: Map<string, number>,
  preferred: readonly string[],
): string[] {
  return [...keys].sort((a, b) => {
    const ia = preferred.indexOf(a);
    const ib = preferred.indexOf(b);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 1e9 : ia) - (ib < 0 ? 1e9 : ib);
    return (shareOf.get(b) ?? 0) - (shareOf.get(a) ?? 0) || a.localeCompare(b);
  });
}

function edgePaths(
  edges: readonly StateEdge[],
  boxes: Record<string, Box>,
): EdgePath[] {
  const out: EdgePath[] = [];
  for (const e of edges) {
    if (e.self || e.from === e.to) continue;
    const a = boxes[e.from];
    const b = boxes[e.to];
    if (!a || !b) continue;
    const y1 = a.y + a.h / 2;
    const y2 = b.y + b.h / 2;
    let d: string;
    let label: EdgePath["label"];
    if (a.lane === b.lane) {
      const x = a.x + a.w;
      const bx = x + 16 + Math.abs(y2 - y1) * 0.12;
      d = `M${r1(x)} ${r1(y1)} C${r1(bx)} ${r1(y1)} ${r1(bx)} ${r1(y2)} ${r1(x)} ${r1(y2)}`;
      label = { x: r1(b.x + b.w - 6), y: r1(b.y + 14), anchor: "end" };
    } else {
      const fwd = b.lane > a.lane || (b.lane === a.lane && b.x > a.x);
      const x1 = fwd ? a.x + a.w : a.x;
      const x2 = fwd ? b.x : b.x + b.w;
      const mx = (x1 + x2) / 2;
      d = `M${r1(x1)} ${r1(y1)} C${r1(mx)} ${r1(y1)} ${r1(mx)} ${r1(y2)} ${r1(x2)} ${r1(y2)}`;
      label = fwd
        ? { x: r1(b.x + 10), y: r1(b.y + 14), anchor: "start" }
        : { x: r1(b.x + b.w - 6), y: r1(b.y + 14), anchor: "end" };
    }
    out.push({ from: e.from, to: e.to, prob: e.prob, d, label });
  }
  return out;
}

/**
 * Lanes by setup, fallback by first stock level, fallback one grid by share.
 * Boxes inside a lane are sorted by share, most common on top.
 */
export function layoutStates(
  nodes: readonly StateNode[],
  edges: readonly StateEdge[],
  lanesBy: LanesBy = "auto",
  opts: LayoutOptions = {},
): StateLayout {
  const width = Math.max(240, Math.floor(opts.width ?? 900));
  const list = [...nodes];
  const mode = pickMode(list, lanesBy);
  const boxes: Record<string, Box> = {};

  if (mode === "grid") {
    const sorted = list.sort(byShare);
    const columns = Math.max(
      1,
      Math.floor((width - PAD * 2 + GAP) / (GRID_BOX + GAP)),
    );
    const w = Math.min(GRID_BOX, width - PAD * 2);
    sorted.forEach((n, i) => {
      const col = i % columns;
      const row = Math.floor(i / columns);
      boxes[n.id] = {
        x: PAD + col * (w + GAP),
        y: GRID_TOP + row * (BOX_H + GAP),
        w,
        h: BOX_H,
        lane: col,
        row,
      };
    });
    const rows = Math.ceil(sorted.length / columns);
    return {
      mode,
      width: Math.max(width, PAD * 2 + columns * (w + GAP) - GAP),
      height: GRID_TOP + rows * (BOX_H + GAP) + 8,
      lanes: [],
      order: sorted.map((n) => n.id),
      columns,
      boxes,
      edges: edgePaths(edges, boxes),
    };
  }

  const groups = new Map<string, StateNode[]>();
  const shareOf = new Map<string, number>();
  for (const n of list) {
    const k = laneKey(n, mode)!;
    groups.set(k, [...(groups.get(k) ?? []), n]);
    shareOf.set(k, (shareOf.get(k) ?? 0) + n.share);
  }
  const preferred =
    mode === "setup"
      ? (opts.order ?? [])
      : levelNames(opts.levels).flatMap((lv) =>
          [...groups.keys()].filter((k) => k.endsWith(`:${lv}`)),
        );
  const keys = laneOrder([...groups.keys()], shareOf, preferred);
  const laneW = Math.max(MIN_LANE, width / keys.length);
  const boxW = Math.min(MAX_BOX, laneW - 34);
  const lanes: Lane[] = keys.map((key, li) => {
    const ids = groups
      .get(key)!
      .sort(byShare)
      .map((n) => n.id);
    ids.forEach((id, row) => {
      boxes[id] = {
        x: r1(li * laneW + (laneW - boxW) / 2),
        y: LANE_TOP + row * (BOX_H + GAP),
        w: r1(boxW),
        h: BOX_H,
        lane: li,
        row,
      };
    });
    const [fam, lv] = key.split(":");
    const title =
      mode === "setup" ? `Press on ${key}` : `${cap(fam)} ${levelWord(lv)}`;
    return { key, title, x: r1(li * laneW), w: r1(laneW), ids };
  });
  const rows = Math.max(0, ...lanes.map((l) => l.ids.length));
  return {
    mode,
    width: Math.max(width, Math.ceil(keys.length * laneW)),
    height: LANE_TOP + rows * (BOX_H + GAP) + 8,
    lanes,
    order: lanes.flatMap((l) => l.ids),
    columns: lanes.length,
    boxes,
    edges: edgePaths(edges, boxes),
  };
}

/** Arrow keys move inside a lane (up, down) and across lanes (left, right). */
export function moveSelection(
  layout: StateLayout,
  id: string,
  key: string,
): string {
  const { order } = layout;
  const box = layout.boxes[id];
  if (!box || !order.length) return id;
  if (key === "Home") return order[0];
  if (key === "End") return order[order.length - 1];
  if (layout.mode === "grid") {
    const i = order.indexOf(id);
    const step =
      key === "ArrowRight"
        ? 1
        : key === "ArrowLeft"
          ? -1
          : key === "ArrowDown"
            ? layout.columns
            : key === "ArrowUp"
              ? -layout.columns
              : 0;
    const j = i + step;
    return j >= 0 && j < order.length ? order[j] : id;
  }
  const lane = layout.lanes[box.lane];
  if (key === "ArrowDown")
    return lane.ids[Math.min(box.row + 1, lane.ids.length - 1)];
  if (key === "ArrowUp") return lane.ids[Math.max(box.row - 1, 0)];
  if (key === "ArrowRight" || key === "ArrowLeft") {
    const dir = key === "ArrowRight" ? 1 : -1;
    for (
      let li = box.lane + dir;
      li >= 0 && li < layout.lanes.length;
      li += dir
    ) {
      const ids = layout.lanes[li].ids;
      if (ids.length) return ids[Math.min(box.row, ids.length - 1)];
    }
  }
  return id;
}

/** Money made on a normal day: profit per run spread over the days of a run. */
export function normalDay(dollarsPerRun: number, horizonDays: number): number {
  return horizonDays > 0 ? dollarsPerRun / horizonDays : 0;
}
