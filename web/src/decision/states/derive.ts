// Pure helpers for the states view: lanes for the map, what to show after the page
// controls, and the money bars. No React here, so vitest can test them.
import type {
  StatesMarkovView,
  StatesStateEdge,
  StatesStateNode,
} from "../types";
import { sentence } from "../viewkit/words";

export type Lane = { name: string; nodes: StatesStateNode[] };
export type Lanes = { kind: "setup" | "level" | "grid"; lanes: Lane[] };

const PART_WORDS: Record<string, string> = {
  revenue: "sales",
  production: "making product",
  setup: "changeovers",
  holding: "keeping stock",
  lost_sales: "lost sales",
  late: "late orders",
};
const PART_ORDER = [
  "revenue",
  "production",
  "setup",
  "holding",
  "lost_sales",
  "late",
];

const byShare = (a: StatesStateNode, b: StatesStateNode): number =>
  b.share - a.share;

function group(
  nodes: StatesStateNode[],
  key: (n: StatesStateNode) => string | null,
  name: (first: StatesStateNode, key: string) => string,
): Lane[] {
  const found = new Map<string, StatesStateNode[]>();
  for (const n of [...nodes].sort(byShare)) {
    const k = key(n);
    if (k === null) continue;
    found.set(k, [...(found.get(k) ?? []), n]);
  }
  const total = (list: StatesStateNode[]) =>
    list.reduce((sum, n) => sum + n.share, 0);
  return [...found.entries()]
    .sort((a, b) => total(b[1]) - total(a[1]))
    .map(([k, list]) => ({ name: name(list[0], k), nodes: list }));
}

/**
 * Lanes come from the setup part of the label, else the first stock level, else one grid
 * sorted by share. A view with no stock levels (the job shop `full` view) is always a grid.
 */
export function lanesOf(nodes: StatesStateNode[], grouped = true): Lanes {
  if (!grouped) {
    return {
      kind: "grid",
      lanes: [{ name: "All states", nodes: [...nodes].sort(byShare) }],
    };
  }
  if (nodes.some((n) => n.parts_of_label?.setup)) {
    const lanes = group(
      nodes,
      (n) => n.parts_of_label?.setup ?? null,
      (first, k) =>
        first.words?.setup || (k === "idle" ? "Rests" : sentence(k)),
    );
    return { kind: "setup", lanes };
  }
  if (nodes.some((n) => n.parts_of_label)) {
    const lanes = group(
      nodes,
      (n) => {
        const first = Object.entries(n.parts_of_label?.levels ?? {})[0];
        return first ? `${first[0]} ${first[1]}` : null;
      },
      (_first, k) => sentence(k),
    );
    return { kind: "level", lanes };
  }
  return {
    kind: "grid",
    lanes: [{ name: "All states", nodes: [...nodes].sort(byShare) }],
  };
}

export const visibleNodes = (
  chain: StatesMarkovView,
  top: number,
): StatesStateNode[] => chain.nodes.slice(0, top);

export const visibleEdges = (
  chain: StatesMarkovView,
  ids: ReadonlySet<string>,
  minProb: number,
): StatesStateEdge[] =>
  chain.edges.filter(
    (e) => ids.has(e.from) && ids.has(e.to) && e.prob >= minProb,
  );

export type MoneyRow = {
  part: string;
  label: string;
  value: number;
  width: number;
};

/** The ledger parts in plain words, with a bar width from 0 to 1. */
export function moneyRows(money: Record<string, number>): MoneyRow[] {
  const parts = PART_ORDER.filter((p) => p in money);
  const biggest = Math.max(1, ...parts.map((p) => Math.abs(money[p])));
  return parts.map((part) => ({
    part,
    label: PART_WORDS[part] ?? part,
    value: money[part],
    width: Math.abs(money[part]) / biggest,
  }));
}

export const nodeTitle = (n: StatesStateNode): string =>
  n.words?.title ?? n.label;
