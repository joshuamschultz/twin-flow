// Plain-word number formats for the decision views (DESIGN.md 8.4: real minus, thousands
// separators). Pure functions, no React.
export const MINUS = "−";

const group = (n: number): string =>
  Math.abs(Math.round(n)).toLocaleString("en-US");

/** Whole dollars: "$1,234" or "−$300". */
export function money(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value))
    return "n/a";
  return `${Math.round(value) < 0 ? MINUS : ""}$${group(value)}`;
}

/** Whole dollars with a sign: "+$1,234" or "−$300". */
export function signedMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value))
    return "n/a";
  return Math.round(value) < 0 ? money(value) : `+${money(value)}`;
}

/** A 0..1 share as a whole percent: "46%". Small shares keep one decimal: "0.4%". */
export function percent(share: number | null | undefined): string {
  if (share === null || share === undefined || Number.isNaN(share))
    return "n/a";
  const pct = share * 100;
  return `${pct > 0 && pct < 1 ? pct.toFixed(1) : Math.round(pct)}%`;
}

/** "1 problem" or "2 problems". */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Capitalise the first letter only. */
export function sentence(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}
