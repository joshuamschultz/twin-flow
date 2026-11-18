// Number words for the decision views. Real minus sign, thousands separators, whole dollars.

export const MINUS = "−";

/** Round half away from zero (Math.round rounds -0.5 up to 0). */
function roundAway(value: number, digits = 0): number {
  const f = 10 ** digits;
  const r = Math.sign(value) * Math.round(Math.abs(value) * f);
  return r / f + 0; // + 0 turns -0 into 0
}

export function num(value: number, digits = 0): string {
  const r = roundAway(value, digits);
  const text = Math.abs(r).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return (r < 0 ? MINUS : "") + text;
}

export function signedNum(value: number, digits = 0): string {
  const r = roundAway(value, digits);
  return (r > 0 ? "+" : "") + num(r, digits);
}

export function usd(value: number, digits = 0): string {
  const r = roundAway(value, digits);
  const text = Math.abs(r).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return (r < 0 ? MINUS : "") + "$" + text;
}

export function signedUsd(value: number, digits = 0): string {
  const r = roundAway(value, digits);
  return (r > 0 ? "+" : "") + usd(r, digits);
}

export function round100(value: number): number {
  return roundAway(value / 100) * 100;
}

/** Axis labels: $20k, +$20k, $2.5k. */
export function usdCompact(value: number, signed = false): string {
  if (value === 0) return "$0";
  const k = Math.abs(value) / 1000;
  const body =
    Math.abs(value) >= 1000
      ? `$${num(k, k % 1 ? 1 : 0)}k`
      : `$${num(Math.abs(value))}`;
  if (value < 0) return MINUS + body;
  return (signed ? "+" : "") + body;
}

export function pct(share: number, digits = 1): string {
  return num(share * 100, digits) + "%";
}

export function joinWords(words: string[]): string {
  if (words.length < 2) return words.join("");
  return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
}
