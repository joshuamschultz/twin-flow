// Pure helpers for the improve view: the run dots and a few plain-word lines.
import { money } from "../viewkit/format";

export type Dot = { gap: number; x: number; y: number };

/**
 * One dot per test run, placed along a strip by the money the new rule made over the old best.
 * Dots that land in the same spot stack upward. `zeroX` is the "no difference" line.
 */
export function dotLayout(
  gaps: number[],
  width: number,
  radius: number,
): { dots: Dot[]; zeroX: number } {
  if (gaps.length === 0) return { dots: [], zeroX: width / 2 };
  const pad = radius + 2;
  const lo = Math.min(0, ...gaps);
  const hi = Math.max(0, ...gaps);
  const span = hi - lo || 1;
  const x = (v: number) => pad + ((v - lo) / span) * (width - 2 * pad);
  const stacked = new Map<number, number>();
  const dots = [...gaps]
    .sort((a, b) => a - b)
    .map((gap) => {
      const cell = Math.round(x(gap) / (radius * 2));
      const level = stacked.get(cell) ?? 0;
      stacked.set(cell, level + 1);
      return { gap, x: x(gap), y: level * (radius * 2 + 1) };
    });
  return { dots, zeroX: x(0) };
}

/** "$1,400 to $5,000", or "not known yet" when there was only one run. */
export function rangeWords(low: number | null, high: number | null): string {
  if (low === null || high === null) return "not known yet";
  return `${money(low)} to ${money(high)}`;
}

export function checkSummary(checks: { pass: boolean }[]): string {
  const passed = checks.filter((c) => c.pass).length;
  return `${passed} of ${checks.length} checks pass`;
}
