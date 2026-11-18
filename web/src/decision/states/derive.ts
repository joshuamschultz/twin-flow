// Pure helpers for the states view: what to show after the page controls, and the money bars. No React here, so vitest can test them.
import type {
  StatesMarkovView,
  StatesStateEdge,
  StatesStateNode,
} from "../types";

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
