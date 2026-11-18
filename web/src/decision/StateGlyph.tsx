// One bar per family, taller for more stock, orange when low (DESIGN.md 3.8).
// An SVG fragment: place it inside an <svg>. Decorative; the box label carries the words.
import { levelInfo, type LevelDef } from "./stateMap";

const NAMED = new Set(["grey", "black", "natural"]);

/** The family colour token, in capsule order (DESIGN.md 8.1). */
export function familyColor(
  family: string,
  order: readonly string[] = [],
): string {
  if (NAMED.has(family)) return `var(--tf-fam-${family})`;
  const i = Math.max(0, order.indexOf(family));
  return `var(--tf-fam-${(i % 5) + 1})`;
}

export const GLYPH_HEIGHT = 24;
const BAR_W = 6;
const BAR_STEP = 9;
const MIN_H = 6;

export function glyphWidth(families: number): number {
  return Math.max(0, families * BAR_STEP - (BAR_STEP - BAR_W));
}

export function StateGlyph({
  levels,
  order,
  levelDefs,
  x = 0,
  y = 0,
}: {
  levels: Record<string, string>;
  order?: readonly string[];
  levelDefs?: readonly (LevelDef | string)[] | null;
  x?: number;
  y?: number;
}) {
  const fams = Object.keys(levels);
  const sorted = order?.length
    ? [...fams].sort(
        (a, b) =>
          (order.indexOf(a) < 0 ? 1e9 : order.indexOf(a)) -
          (order.indexOf(b) < 0 ? 1e9 : order.indexOf(b)),
      )
    : fams;
  return (
    <g
      transform={`translate(${x},${y})`}
      aria-hidden="true"
      className="tf-sm-glyph"
    >
      {sorted.map((fam, i) => {
        const info = levelInfo(levels[fam], levelDefs);
        const step =
          info.index < 0 ? 0.5 : info.index / Math.max(1, info.count - 1);
        const h = MIN_H + step * (GLYPH_HEIGHT - MIN_H);
        return (
          <rect
            key={fam}
            data-family={fam}
            data-level={levels[fam]}
            x={i * BAR_STEP}
            y={GLYPH_HEIGHT - h}
            width={BAR_W}
            height={h}
            rx={1}
            fill={info.low ? "var(--tf-out)" : familyColor(fam, order)}
          />
        );
      })}
      <line
        x1={-2}
        x2={glyphWidth(sorted.length) + 2}
        y1={GLYPH_HEIGHT + 1}
        y2={GLYPH_HEIGHT + 1}
        stroke="var(--tf-axis)"
      />
    </g>
  );
}
