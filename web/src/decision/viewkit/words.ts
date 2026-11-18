// Small word helpers the states, improve and project views share. Number formats come from
// ../format.ts.
import { pct } from "../format";

/** "1 problem" or "2 problems". */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** A 0..1 share as a whole percent, with one decimal under 1%: "46%", "0.7%". */
export function share(value: number): string {
  return pct(value, value > 0 && value < 0.01 ? 1 : 0);
}

export function sentence(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}
