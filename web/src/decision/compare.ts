// Compare two rules (DESIGN.md 3.5). Pure: the MEASURES table, the rows and the sentence.
import { num, pct, signedNum, signedUsd, usd } from "./format";
import type { PolicyRow } from "./types";

export type Better = "up" | "down" | "none";
export type Tone = "g" | "l" | "n";

export type Measure = {
  id: string;
  group: "Result" | "On the floor" | "Where the money goes, per run";
  label: string;
  /** Which way is better. "none": neither is better by itself. */
  better: Better;
  get: (p: PolicyRow, horizonDays: number) => number | null;
  format: (v: number) => string;
  formatDelta: (v: number) => string;
  /** Drop the row when both rules have zero (late cost in a race without due dates). */
  hideWhenZero?: boolean;
};

const one = (v: number) => num(v, 1);
const part = (
  id: string,
  label: string,
  better: Better,
  hideWhenZero = false,
): Measure => ({
  id,
  group: "Where the money goes, per run",
  label,
  better,
  get: (p) => p.parts[id] ?? null,
  format: usd,
  formatDelta: signedUsd,
  hideWhenZero,
});

/** The one place that says which direction is better for each measure. */
export const MEASURES: Measure[] = [
  {
    id: "profit",
    group: "Result",
    label: "Profit per run",
    better: "up",
    get: (p) => p.dollars.mean,
    format: usd,
    formatDelta: signedUsd,
  },
  {
    id: "fill_rate",
    group: "Result",
    label: "Orders filled from stock",
    better: "up",
    get: (p) => (p.fill_rate === null ? null : p.fill_rate),
    format: (v) => pct(v),
    // toFixed(9) drops float noise (0.9437 - 0.9402 is 0.00349999...).
    formatDelta: (v) => signedNum(Number((v * 100).toFixed(9)), 1) + " pts",
  },
  {
    id: "changeovers",
    group: "On the floor",
    label: "Changeovers per run",
    better: "down",
    get: (p) => p.changeovers,
    format: one,
    formatDelta: (v) => signedNum(v, 1),
  },
  {
    id: "cross_family",
    group: "On the floor",
    label: "Changeovers to another family",
    better: "down",
    get: (p) => p.cross_family,
    format: one,
    formatDelta: (v) => signedNum(v, 1),
  },
  {
    id: "avg_stock",
    group: "On the floor",
    label: "Stock on hand (units)",
    better: "none",
    get: (p) => p.avg_stock,
    format: (v) => num(v),
    formatDelta: (v) => signedNum(v),
  },
  {
    id: "rest_days",
    group: "On the floor",
    label: "Days the press rests",
    better: "none",
    get: (p, days) => (p.idle_share === null ? null : p.idle_share * days),
    format: one,
    formatDelta: (v) => signedNum(v, 1),
  },
  part("revenue", "Sales", "up"),
  part("production", "Making product", "down"),
  part("setup", "Changeovers", "down"),
  part("holding", "Keeping stock", "down"),
  part("lost_sales", "Lost sales", "down"),
  part("late", "Late orders", "down", true),
];

export function toneOf(delta: number, better: Better): Tone {
  if (Math.abs(delta) < 1e-9 || better === "none") return "n";
  return (better === "up" ? delta > 0 : delta < 0) ? "g" : "l";
}

export type CompareRow = {
  id: string;
  group: Measure["group"];
  label: string;
  a: number | null;
  b: number | null;
  aText: string;
  bText: string;
  /** Bar widths as a share of the larger absolute value, 0 to 1. */
  aShare: number;
  bShare: number;
  delta: number | null;
  deltaText: string;
  tone: Tone;
};

const NOT_TRACKED = "not kept";

export function compareRows(
  a: PolicyRow,
  b: PolicyRow,
  horizonDays: number,
): CompareRow[] {
  const rows: CompareRow[] = [];
  for (const m of MEASURES) {
    const va = m.get(a, horizonDays);
    const vb = m.get(b, horizonDays);
    if (va === null && vb === null) continue;
    if (m.hideWhenZero && !va && !vb) continue;
    const both = va !== null && vb !== null;
    const delta = both ? va - vb : null;
    const max = Math.max(Math.abs(va ?? 0), Math.abs(vb ?? 0)) || 1;
    const same = delta !== null && m.format(va!) === m.format(vb!);
    rows.push({
      id: m.id,
      group: m.group,
      label: m.label,
      a: va,
      b: vb,
      aText: va === null ? NOT_TRACKED : m.format(va),
      bText: vb === null ? NOT_TRACKED : m.format(vb),
      aShare: Math.abs(va ?? 0) / max,
      bShare: Math.abs(vb ?? 0) / max,
      delta,
      deltaText: delta === null ? "" : same ? "same" : m.formatDelta(delta),
      tone: delta === null || same ? "n" : toneOf(delta, m.better),
    });
  }
  return rows;
}

/** Runs where A made more money than B on the same orders. */
export function runsWon(a: PolicyRow, b: PolicyRow): number {
  const bs = b.dollars_by_run ?? [];
  return (a.dollars_by_run ?? []).filter((v, i) => i < bs.length && v > bs[i])
    .length;
}

export function compareSentence(
  a: PolicyRow,
  b: PolicyRow,
  runs: number,
): string {
  if (a.policy === b.policy) return "Pick two different rules.";
  const d = a.dollars.mean - b.dollars.mean;
  const money = usd(Math.abs(d));
  const head =
    money === "$0"
      ? `${a.label} makes the same per run as ${b.label}.`
      : `${a.label} makes ${money} ${d > 0 ? "more" : "less"} per run than ${b.label}.`;
  const paired =
    (a.dollars_by_run?.length ?? 0) > 0 && (b.dollars_by_run?.length ?? 0) > 0;
  if (!paired) return head;
  return `${head} It won ${runsWon(a, b)} of ${runs} runs on the same orders.`;
}
