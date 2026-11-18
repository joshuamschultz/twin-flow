// Small formatters for the state map and money leaks.
// Swap for the shared format.ts (B-06) once it lands; the output rules are the same:
// thousands separators, a real minus sign, no trailing ".0".
const MINUS = "−";

export function usd(value: number): string {
  const v = Math.round(value);
  const text = `$${Math.abs(v).toLocaleString("en-US")}`;
  return v < 0 ? `${MINUS}${text}` : text;
}

/** Share as a percent: 0.0854 -> "8.5%", 0.722 with digits 0 -> "72%". */
export function pct(share: number, digits = 1): string {
  const v = share * 100;
  const text = v.toFixed(digits);
  return `${digits > 0 ? text.replace(/\.0+$/, "") : text}%`;
}

/** Money made on a normal day: profit per run spread over the days of a run. */
export function normalDay(dollarsPerRun: number, horizonDays: number): number {
  return horizonDays > 0 ? dollarsPerRun / horizonDays : 0;
}
