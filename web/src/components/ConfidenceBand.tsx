import type { Interval } from "../api";

/**
 * Renders an interval as a range bar with a mean tick — the product's
 * central visual idiom. Confidence is shown as a filled range, not a
 * single point estimate, so every KPI reads as "somewhere in here"
 * rather than "exactly this".
 */
interface ConfidenceBandProps {
  interval: Interval;
  domainMin?: number;
  domainMax?: number;
  unit?: string;
  precision?: number;
  compact?: boolean;
  level?: number;
}

function clampPct(value: number, min: number, max: number): number {
  if (max <= min) return 0;
  return Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
}

function fmt(value: number, precision: number): string {
  return value.toFixed(precision);
}

export function ConfidenceBand({
  interval,
  domainMin = 0,
  domainMax = 100,
  unit = "%",
  precision = 1,
  compact = false,
  level = 95,
}: ConfidenceBandProps) {
  const loPct = clampPct(interval.lo, domainMin, domainMax);
  const hiPct = clampPct(interval.hi, domainMin, domainMax);
  const meanPct = clampPct(interval.mean, domainMin, domainMax);
  const width = Math.max(hiPct - loPct, 0.6);

  const track = (
    <div className="band-track">
      <div className="band-range" style={{ left: `${loPct}%`, width: `${width}%` }} />
      <div className="band-mean" style={{ left: `${meanPct}%` }} />
    </div>
  );

  if (compact) {
    return (
      <div className="band-inline">
        {track}
        <span className="band-readout">
          {fmt(interval.mean, precision)}
          {unit} [{fmt(interval.lo, precision)}–{fmt(interval.hi, precision)}
          {unit}]
        </span>
      </div>
    );
  }

  return (
    <div className="band">
      {track}
      <div className="band-labels">
        <span>
          lo {fmt(interval.lo, precision)}
          {unit}
        </span>
        <span>
          hi {fmt(interval.hi, precision)}
          {unit}
        </span>
      </div>
      <div className="band-legend">
        n={interval.n} replications · {level}% band · median{" "}
        {fmt(interval.p50, precision)}
        {unit}
      </div>
    </div>
  );
}
