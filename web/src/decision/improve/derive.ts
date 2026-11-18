// Pure helpers for the improve view: plain-word lines.
import { usd } from "../format";

/** "$1,400 to $5,000", or "not known yet" when there was only one run. */
export function rangeWords(low: number | null, high: number | null): string {
  if (low === null || high === null) return "not known yet";
  return `${usd(low)} to ${usd(high)}`;
}

export function checkSummary(checks: { pass: boolean }[]): string {
  const passed = checks.filter((c) => c.pass).length;
  return `${passed} of ${checks.length} checks pass`;
}
