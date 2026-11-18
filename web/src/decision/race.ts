// Pure helpers for the race view: words, layouts and series (DESIGN.md section 3).
// Sentences that state a result come from Python; these only arrange them or add the
// small glue words a chart needs.
import { joinWords, num, pct, round100, signedUsd, usd } from "./format";
import type { Caution, FloorRow, PolicyRow, RaceViewModel } from "./types";

/* ── Small words ──────────────────────────────────────────────────── */

const WORDS = [
  "zero",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
  "ten",
];
export function numberWord(n: number): string {
  return Number.isInteger(n) && n >= 0 && n <= 10 ? WORDS[n] : num(n);
}
const capital = (t: string) => (t ? t[0].toUpperCase() + t.slice(1) : t);

/** Families named after a colour keep that colour (owner decision 4). Others take the
 *  theme series in capsule order. Always shown with a text label too. */
const LITERAL = new Set(["grey", "gray", "black", "natural"]);
export function familyColor(name: string, index: number): string {
  const key = name.toLowerCase() === "gray" ? "grey" : name.toLowerCase();
  if (LITERAL.has(key)) return `var(--tf-fam-${key})`;
  return `var(--tf-fam-${(index % 5) + 1})`;
}
export function familiesAreColours(families: string[]): boolean {
  return (
    families.length > 0 && families.every((f) => LITERAL.has(f.toLowerCase()))
  );
}

/* ── Hero ─────────────────────────────────────────────────────────── */

export type LedePart = { text: string; strong?: boolean };
export type Hero = {
  kind: "switch" | "keep" | "maybe" | "worse" | "nobase";
  before: string;
  name: string;
  after: string;
  lede: LedePart[];
  perRun: string | null;
  /** The 60-day yearly guess: per run times runs per year. */
  perYear: string | null;
  /** The yearly figure the hero leads with: the long check when there is one. */
  year: { value: string; label: string; checked: boolean } | null;
  /** The 60-day guess as secondary text, when the long check leads. */
  quickYear: string | null;
  horizon: number;
  badge: string | null;
  level: string;
  range: string | null;
  note: string | null;
};

export function pickRow(m: RaceViewModel): PolicyRow | undefined {
  return m.policies.find((p) => p.policy === m.recommendation.policy);
}
export function baseRow(m: RaceViewModel): PolicyRow | undefined {
  return m.baseline
    ? m.policies.find((p) => p.policy === m.baseline!.policy)
    : undefined;
}

export function hero(m: RaceViewModel): Hero {
  const name = m.recommendation.label ?? m.recommendation.policy ?? "";
  const horizon = m.about.horizon_days;
  const c = m.confidence;
  const d = m.delta;
  const level = c?.level ?? "unknown";
  let note: string | null = null;
  if (m.policies.length === 1)
    note = "Only one rule ran. Add a second rule to compare.";
  else if (!m.about.score_is_dollars)
    note = `Ranked by ${m.about.score}. Dollars shown for scale.`;

  if (!m.baseline || !d || !c) {
    return {
      kind: "nobase",
      before: "",
      name,
      after: " made the most money.",
      lede: [
        {
          text: "We do not know what you do today, so we cannot show the gain. Set experiment.race.baseline in your project file.",
        },
      ],
      perRun: null,
      perYear: null,
      year: null,
      quickYear: null,
      horizon,
      badge: null,
      level,
      range: null,
      note:
        note ??
        "Set experiment.race.baseline in your project file to compare with today.",
    };
  }
  const base = m.baseline.label;
  const more = d.per_run >= 0 ? "more" : "less";
  const lede: LedePart[] = m.recommendation.same_as_baseline
    ? [{ text: m.recommendation.why }]
    : [
        { text: "It makes " },
        { text: `${usd(Math.abs(d.per_run))} ${more}`, strong: true },
        {
          text: ` in each ${horizon}-day run than what you do today (${base}). It won `,
        },
        { text: `${c.wins} of ${c.runs}`, strong: true },
        { text: " test runs on the same customer orders." },
      ];
  let words: Pick<Hero, "kind" | "before" | "after">;
  if (m.recommendation.same_as_baseline || level === "same_policy")
    words = {
      kind: "keep",
      before: "Keep ",
      after: ". Nothing tested beats it.",
    };
  else if (level === "very_likely_better")
    words = { kind: "switch", before: "Switch to ", after: "." };
  else if (level === "very_likely_worse")
    words = {
      kind: "worse",
      before: "",
      after: " ranks first, but makes less money.",
    };
  else
    words = {
      kind: "maybe",
      before: "",
      after: " may be better. Test more before you switch.",
    };
  const same = words.kind === "keep";
  const range =
    !same && c.low !== null && c.high !== null
      ? `The gain is likely between ${usd(round100(c.low))} and ${usd(round100(c.high))} per run.`
      : null;
  const perYear = same ? null : signedUsd(round100(d.per_year));
  const long = same ? null : d.long_check;
  const year = long
    ? {
        value: signedUsd(round100(long.per_year)),
        label: `more profit per year, from a ${long.days}-day check`,
        checked: true,
      }
    : perYear
      ? {
          value: perYear,
          label: `more per year, if every ${horizon} days looks like these runs`,
          checked: false,
        }
      : null;
  return {
    ...words,
    name,
    lede,
    perRun: same ? null : signedUsd(d.per_run),
    perYear,
    year,
    quickYear:
      long && perYear
        ? `Quick math from the ${horizon}-day runs says ${perYear} a year.`
        : null,
    horizon,
    badge: c.words,
    level,
    range,
    note,
  };
}

export function cautionLine(m: RaceViewModel): {
  title: string;
  text: string;
  link: { to: string; label: string };
} | null {
  const top: Caution | undefined = m.cautions?.[0];
  if (!top) return null;
  const first = top.body.match(/^.*?[.!?](\s|$)/)?.[0].trim() ?? top.body;
  const link =
    top.key === "late_gain"
      ? { to: "why", label: "See when it pulls ahead." }
      : { to: "cautions", label: "See all cautions." };
  return { title: top.title.replace(/\.?$/, "."), text: first, link };
}

export function floorValues(row: FloorRow): [string, string] {
  switch (row.unit) {
    case "share":
      return [pct(row.baseline), pct(row.recommended)];
    case "units":
      return [`${num(row.baseline)} units`, num(row.recommended)];
    default:
      return [num(row.baseline), num(row.recommended)];
  }
}

/* ── Run dots ─────────────────────────────────────────────────────── */

export type Dot = { x: number; y: number; gap: number; index: number };

/** A scale from dollars to px that always shows zero, with round tick steps. */
export function dollarScale(values: number[], width: number, pad: number) {
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const span = hi - lo || 1;
  const raw = span / 5;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step =
    [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? raw;
  const min = Math.floor(lo / step) * step;
  const max = Math.ceil(hi / step) * step;
  const x = (v: number) =>
    pad + ((v - min) / (max - min || 1)) * (width - 2 * pad);
  const ticks: number[] = [];
  for (let t = min; t <= max + step / 2; t += step) ticks.push(Math.round(t));
  return { x, min, max, step, ticks };
}

/** Dots binned into columns one dot wide and stacked up, so none overlap. */
export function dotLayout(
  gaps: number[],
  width: number,
  {
    r = 3.6,
    pad = 8,
    base = 0,
  }: { r?: number; pad?: number; base?: number } = {},
) {
  const scale = dollarScale(gaps, width, pad);
  const col = 2 * r + 1.8;
  const heights = new Map<number, number>();
  const order = gaps.map((g, i) => ({ g, i })).sort((a, b) => a.g - b.g);
  const dots: Dot[] = order.map(({ g, i }) => {
    const bin = Math.round(scale.x(g) / col);
    const h = heights.get(bin) ?? 0;
    heights.set(bin, h + 1);
    return { x: bin * col, y: base - r - h * col, gap: g, index: i };
  });
  const tallest = Math.max(0, ...heights.values());
  return { dots, scale, tallest, col };
}

export function dotsCaption(gaps: number[]): string {
  const left = gaps.filter((g) => g < 0).length;
  const n = gaps.length;
  if (left === 0)
    return `Each dot is one test run. All ${n} dots are right of zero, so the new rule won every time.`;
  return `Each dot is one test run. ${left} of ${n} dots are left of zero. In those runs today's rule did better.`;
}

/* ── Waterfall ────────────────────────────────────────────────────── */

const PART_NAMES: Record<string, [string, string]> = {
  // [when the part adds profit, when it takes profit away]
  setup: ["Fewer changeovers", "More changeovers"],
  revenue: ["More sales", "Less sales"],
  lost_sales: ["Fewer lost sales", "More lost sales"],
  production: ["Making less product", "Making more product"],
  holding: ["Keeping less stock", "Keeping more stock"],
  late: ["Fewer late orders", "More late orders"],
};

export function waterfall(m: RaceViewModel) {
  const items = m.drivers
    .filter((d) => d.gain !== 0)
    .map((d) => ({
      label: PART_NAMES[d.part]?.[d.gain > 0 ? 0 : 1] ?? d.part,
      value: d.gain,
    }))
    .sort((a, b) => b.value - a.value);
  let run = 0;
  const placed = items.map((i) => {
    const start = run;
    run += i.value;
    return { ...i, start, end: run };
  });
  const ends = [0, ...placed.map((p) => p.end)];
  return {
    items: placed,
    total: m.delta?.per_run ?? run,
    min: Math.min(...ends),
    max: Math.max(...ends) * 1.02,
  };
}

/* ── Gap line ─────────────────────────────────────────────────────── */

export type Series = {
  id: string;
  label: string;
  values: number[];
  emphasis: boolean;
};

export function gapSeries(m: RaceViewModel): Series[] {
  const base = baseRow(m);
  const pick = pickRow(m);
  if (!base || !pick || !m.timing) return [];
  const others = m.policies
    .filter((p) => p.policy !== pick.policy && p.policy !== base.policy)
    .sort((a, b) => a.rank - b.rank)
    .slice(0, 2);
  const diff = (p: PolicyRow) =>
    p.profit_by_day.mean.map((v, i) => v - (base.profit_by_day.mean[i] ?? 0));
  return [
    {
      id: pick.policy,
      label: pick.label,
      values: m.timing.gap_by_day.mean,
      emphasis: true,
    },
    ...others.map((p) => ({
      id: p.policy,
      label: p.label,
      values: diff(p),
      emphasis: false,
    })),
  ];
}

/** Spread label positions so no two are closer than `gap` px, keeping their order. */
export function labelEnds(ys: number[], gap: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  for (let k = 1; k < order.length; k++)
    if (order[k].y - order[k - 1].y < gap) order[k].y = order[k - 1].y + gap;
  const out = new Array<number>(ys.length);
  for (const o of order) out[o.i] = o.y;
  return out;
}

export function pullAheadTakeaway(m: RaceViewModel): string {
  const pick = pickRow(m);
  const base = baseRow(m);
  const words = m.timing?.words ?? "";
  if (!pick || !base || pick.stock_end == null || base.stock_end == null)
    return words;
  const a = num(pick.stock_end);
  const b = num(base.stock_end);
  const tail =
    pick.stock_end >= base.stock_end
      ? ` It does not win by emptying the shelf: it ends with ${a} units, today's rule with ${b}.`
      : ` It ends with fewer units on the shelf than today's rule: ${a} against ${b}.`;
  return words + tail;
}

/* ── Leaderboard ──────────────────────────────────────────────────── */

const REFERENCE_FLOORS = new Set(["random", "idle"]); // built-in reference rules

export function axisLimit(rows: PolicyRow[]): number {
  const reach = Math.max(
    1,
    ...rows.flatMap((p) =>
      p.vs_baseline
        ? [Math.abs(p.vs_baseline.mean), Math.abs(p.vs_baseline.high ?? 0)]
        : [],
    ),
  );
  const step = reach > 20000 ? 2000 : reach > 5000 ? 1000 : 100;
  return Math.ceil((reach * 1.04) / step) * step;
}

export type BoardRow = {
  id: string;
  rank: number;
  label: string;
  about: string;
  pick: boolean;
  today: boolean;
  mean: number;
  low: number | null;
  high: number | null;
  value: string;
  tone: "g" | "l" | "n" | "";
  tip: string;
};

export function leaderboard(m: RaceViewModel, foldAfter = 8) {
  const runs = m.confidence?.runs ?? 0;
  const rows: BoardRow[] = [...m.policies]
    .sort((a, b) => a.rank - b.rank)
    .map((p) => {
      const today = p.is_baseline;
      const v = p.vs_baseline;
      const mean = today || !v ? 0 : v.mean;
      const same = !today && Math.abs(mean) < 1;
      const range =
        v && v.low !== null && v.high !== null
          ? ` Likely range ${signedUsd(round100(v.low))} to ${signedUsd(round100(v.high))}.`
          : "";
      return {
        id: p.policy,
        rank: p.rank,
        label: p.label,
        about: p.about,
        pick: p.policy === m.recommendation.policy,
        today,
        mean,
        low: today ? null : (v?.low ?? null),
        high: today ? null : (v?.high ?? null),
        value: today
          ? `${usd(p.dollars.mean)} now`
          : same
            ? "same as today"
            : signedUsd(mean),
        tone: today ? "" : same ? "n" : mean > 0 ? "g" : "l",
        tip: today
          ? `${p.label} is what you do today. It makes ${usd(p.dollars.mean)} per run.`
          : `${p.label}: ${signedUsd(mean)} per run vs today.${range} Won ${p.wins_vs_baseline} of ${runs} runs.`,
      };
    });
  const main = rows.filter((r) => r.rank <= foldAfter);
  const rest = rows.filter((r) => r.rank > foldAfter);
  const floors = rest.filter((r) => REFERENCE_FLOORS.has(r.id));
  return {
    main,
    rest,
    floors: floors.length,
    foldLabel:
      `Show ${numberWord(rest.length)} weaker rules` +
      (floors.length
        ? ` (${numberWord(floors.length)} ${floors.length === 1 ? "is a reference floor" : "are reference floors"}: ${joinWords(floors.map((f) => f.label))})`
        : ""),
    limit: axisLimit(m.policies.filter((p) => p.rank <= foldAfter)),
  };
}

export function beatToday(m: RaceViewModel): string {
  const base = baseRow(m);
  if (!base) return "";
  // "Beats today" means the whole likely range is above zero, not only the average.
  const better = m.policies.filter(
    (p) => !p.is_baseline && (p.vs_baseline?.low ?? 0) > 0,
  );
  const same = m.policies.filter(
    (p) => !p.is_baseline && p.vs_baseline && Math.abs(p.vs_baseline.mean) < 1,
  );
  const n = better.length;
  const parts: string[] = [];
  if (n === 0) parts.push("No rule beats today.");
  else
    parts.push(
      `${capital(numberWord(n))} ${n === 1 ? "rule beats" : "rules beat"} today.`,
    );
  if (
    n > 1 &&
    better.every((p) => (p.cross_family ?? 0) < (base.cross_family ?? 0))
  ) {
    const verb = familiesAreColours(m.families)
      ? "switch colours less"
      : "change family less often";
    parts.push(`All ${numberWord(n)} ${verb}.`);
  }
  for (const p of same) {
    const own = p.source === "project" ? " is your own file and" : "";
    parts.push(
      `${p.label}${own} acts the same as ${base.label}, so it scores the same.`,
    );
  }
  return parts.join(" ");
}
