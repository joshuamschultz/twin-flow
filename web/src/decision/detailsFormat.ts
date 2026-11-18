// Small formatters for the tuning and details sections (DESIGN.md 8.4: real minus sign,
// thousands separators, whole dollars).
import type { Settings } from "./types";

const MINUS = "−";
const DASH = "–";

export function fmtMoney(
  value: number,
  opts: { signed?: boolean } = {},
): string {
  const rounded = Math.round(value);
  const body = `$${Math.abs(rounded).toLocaleString("en-US")}`;
  if (rounded < 0) return `${MINUS}${body}`;
  return opts.signed && rounded > 0 ? `+${body}` : body;
}

export function fmtPct(share: number | null | undefined, digits = 1): string {
  return share === null || share === undefined
    ? DASH
    : `${(share * 100).toFixed(digits)}%`;
}

export function fmtNum(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

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

/** Axis money: $60k, $1.5k, $400. */
export function fmtMoneyShort(value: number): string {
  const rounded = Math.round(value);
  if (Math.abs(rounded) < 1000) return fmtMoney(rounded);
  const k = Number((Math.abs(rounded) / 1000).toFixed(1));
  return `${rounded < 0 ? MINUS : ""}$${k}k`;
}

/** A setting value short enough for an axis label: 1000000000 becomes "1B". */
export function shortValue(label: string): string {
  const n = Number(label);
  if (label.trim() === "" || Number.isNaN(n) || Math.abs(n) < 10000)
    return label;
  return new Intl.NumberFormat("en-US", { notation: "compact" }).format(n);
}
