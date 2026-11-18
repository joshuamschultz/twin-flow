// What if (DESIGN.md 3.6). Pure re-pricing of the stored per-rule cost parts.
// profit = revenue - production - setup x m1 - holding x m2 - lost_sales x m3 - late
import type { PolicyRow, WhatIf } from "./types";

/** Cost multiplier per part name. A part not named keeps its real cost (1). */
export type Multipliers = Record<string, number>;

export type RankedRow = {
  policy: string;
  label: string;
  score: number;
  /** Places moved from the real ranking. Positive: moved up. */
  move: number;
};

export type Flip = { at: number; leader: string } | null;
export type FlipPoints = Record<string, { below: Flip; above: Flip }>;

/** Profit per run with each cost part scaled by its multiplier. */
export function scoreAt(p: PolicyRow, m: Multipliers): number {
  let total = 0;
  for (const [part, value] of Object.entries(p.parts)) {
    total += part === "revenue" ? value : -value * (m[part] ?? 1);
  }
  return total;
}

function priced(policies: PolicyRow[]): PolicyRow[] {
  return [...policies]
    .filter((p) => typeof p.parts?.revenue === "number")
    .sort((a, b) => a.rank - b.rank);
}

/** The ranking at these costs. Ties keep the real order. */
export function rerank(policies: PolicyRow[], m: Multipliers): RankedRow[] {
  const real = priced(policies);
  return real
    .map((p, i) => ({ p, i, score: scoreAt(p, m) }))
    .sort((x, y) => y.score - x.score || x.i - y.i)
    .map(({ p, i, score }, n) => ({
      policy: p.policy,
      label: p.label,
      score,
      move: i - n,
    }));
}

function leaderAt(real: PolicyRow[], m: Multipliers): string {
  let best = real[0];
  let bestScore = scoreAt(best, m);
  for (const p of real.slice(1)) {
    const s = scoreAt(p, m);
    if (s > bestScore) [best, bestScore] = [p, s];
  }
  return best.policy;
}

/**
 * For each slider, the first value (scanning out from 1 in half steps) where a rule
 * other than `pick` leads, with the other sliders at 1. Null when the pick holds.
 */
export function flipPoints(
  policies: PolicyRow[],
  pick: string,
  config: Pick<WhatIf, "parts" | "min" | "max" | "step">,
): FlipPoints {
  const real = priced(policies);
  const half = config.step / 2;
  const scan = (part: string, dir: 1 | -1): Flip => {
    if (!(half > 0) || real.length === 0) return null;
    for (let i = 1; ; i++) {
      const at = Math.round((1 + dir * i * half) * 1e6) / 1e6;
      if (at < config.min - 1e-9 || at > config.max + 1e-9) return null;
      const leader = leaderAt(real, { [part]: at });
      if (leader !== pick) return { at, leader };
    }
  };
  const out: FlipPoints = {};
  for (const part of config.parts) {
    out[part] = { below: scan(part, -1), above: scan(part, 1) };
  }
  return out;
}
