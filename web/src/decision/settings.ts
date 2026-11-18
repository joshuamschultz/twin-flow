// Words for settings, rules and counts. Money and numbers come from format.ts.
import type { Settings } from "./types";

const DASH = "\u2013";

export function settingName(key: string): string {
  return key.replace(/_/g, " ");
}

export function settingValue(value: Settings[string]): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value === null) return DASH;
  return String(typeof value === "number" ? Number(value.toFixed(4)) : value);
}

/** One setting for people: {target_days: 14} becomes "target days 14". */
export function settingText(settings: Settings | null | undefined): string {
  if (!settings) return DASH;
  const parts = Object.entries(settings).map(
    ([k, v]) => `${settingName(k)} ${settingValue(v)}`,
  );
  return parts.length ? parts.join(", ") : "no settings";
}

/** A setting value short enough for an axis label: 1000000000 becomes "1B". */
export function shortValue(label: string): string {
  const n = Number(label);
  if (label.trim() === "" || Number.isNaN(n) || Math.abs(n) < 10000)
    return label;
  return new Intl.NumberFormat("en-US", { notation: "compact" }).format(n);
}

/** A rule's name for people. Uses the model's label when the rule is in `policies`. */
export function ruleLabel(
  policy: string,
  policies: ReadonlyArray<{ policy: string; label: string }> = [],
): string {
  const found = policies.find((p) => p.policy === policy);
  if (found) return found.label;
  const text = policy.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}
