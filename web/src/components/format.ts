/** Shared number-formatting helpers for KPI displays. */

/** Seconds -> a compact human duration ("45s", "3.2m", "1.8h"), sign preserved. */
export function formatDuration(seconds: number): string {
  const sign = seconds < 0 ? "-" : "";
  const abs = Math.abs(seconds);
  if (abs < 60) return `${sign}${abs.toFixed(0)}s`;
  if (abs < 3600) return `${sign}${(abs / 60).toFixed(1)}m`;
  return `${sign}${(abs / 3600).toFixed(1)}h`;
}
