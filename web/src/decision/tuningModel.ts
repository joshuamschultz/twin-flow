// Pure helpers behind TuningCurve and TuningTable (DESIGN.md 3.10).
// The model decides "touchy" and writes the sentence. Nothing is recomputed here.
import {
  ruleLabel,
  settingName,
  settingText,
  settingValue,
} from "./detailsFormat";
import type { RaceViewModel, Settings, Trial, Tuning } from "./types";

export type CurvePoint = {
  label: string;
  dollars: number | null;
  chosen: boolean;
  problem: string | null;
  settings: Settings;
};

export type CurvePanel = { title: string | null; points: CurvePoint[] };

export type CurveSpec = {
  xName: string;
  groupName: string | null;
  panels: CurvePanel[];
  skipped: CurvePoint[];
  yMin: number;
  yMax: number;
  ticks: number[];
};

export type TuningRow = {
  policy: string;
  label: string;
  isPick: boolean;
  tried: number;
  best: string;
  spread: number;
  touchy: boolean;
  pill: "steady" | "touchy";
};

type Value = Settings[string];

const byValue = (a: Value, b: Value): number =>
  typeof a === "number" && typeof b === "number" ? a - b : 0;

function keysOf(trials: Trial[]): string[] {
  const seen: string[] = [];
  for (const t of trials)
    for (const k of Object.keys(t.settings))
      if (!seen.includes(k)) seen.push(k);
  return seen;
}

function distinct(trials: Trial[], key: string): Value[] {
  const out: Value[] = [];
  for (const t of trials)
    if (!out.includes(t.settings[key])) out.push(t.settings[key]);
  return out;
}

function niceStep(raw: number): number {
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  return (f <= 1.5 ? 1 : f <= 3 ? 2 : f <= 7 ? 5 : 10) * pow;
}

function scale(values: number[]): Pick<CurveSpec, "yMin" | "yMax" | "ticks"> {
  if (!values.length) return { yMin: 0, yMax: 1, ticks: [0, 1] };
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || Math.abs(hi) * 0.1 || 1;
  const yMin = lo - span * 0.15;
  const yMax = hi + span * 0.15;
  const step = niceStep((yMax - yMin) / 4);
  const ticks: number[] = [];
  for (let v = Math.ceil(yMin / step) * step; v <= yMax; v += step)
    ticks.push(v);
  return { yMin, yMax, ticks };
}

function point(t: Trial, key: string): CurvePoint {
  return {
    label: settingValue(t.settings[key]),
    dollars: t.dollars,
    chosen: t.chosen,
    problem: t.problem,
    settings: t.settings,
  };
}

/** What to draw: one line, or one small line per value of a second setting. */
export function curveSpec(tuning: Tuning): CurveSpec {
  const keys = keysOf(tuning.trials);
  if (!keys.length) {
    return {
      xName: "",
      groupName: null,
      panels: [],
      skipped: [],
      ...scale([]),
    };
  }
  const ranked = [...keys].sort(
    (a, b) =>
      distinct(tuning.trials, b).length - distinct(tuning.trials, a).length,
  );
  const xKey = ranked[0];
  const gKey = ranked.length > 1 ? ranked[1] : null;
  const held = ranked.slice(2);
  const trials = tuning.trials.filter((t) =>
    held.every((k) => !tuning.chosen || t.settings[k] === tuning.chosen[k]),
  );
  const groups = gKey ? [...distinct(trials, gKey)].sort(byValue) : [null];
  const panels: CurvePanel[] = groups.map((g) => {
    const rows = trials.filter((t) => gKey === null || t.settings[gKey] === g);
    const points = rows
      .filter((t) => t.dollars !== null)
      .map((t) => point(t, xKey));
    points.sort((a, b) => byValue(a.settings[xKey], b.settings[xKey]));
    return {
      title:
        gKey === null
          ? null
          : `${settingName(gKey)} ${settingValue(g as Value)}`,
      points,
    };
  });
  const skipped = trials
    .filter((t) => t.dollars === null)
    .map((t) => point(t, xKey));
  const dollars = panels.flatMap((p) =>
    p.points.map((q) => q.dollars as number),
  );
  return {
    xName: settingName(xKey),
    groupName: gKey && settingName(gKey),
    panels,
    skipped,
    ...scale(dollars),
  };
}

type RowSource = Pick<RaceViewModel, "tuning" | "policies" | "recommendation">;

/** One table row per tuned rule: the pick first, then by rank. */
export function tuningRows(source: RowSource): TuningRow[] {
  const pick = source.recommendation.policy;
  const rankOf = (policy: string) =>
    source.policies.find((p) => p.policy === policy)?.rank ?? 1e9;
  const rows = source.tuning.map((t) => ({
    policy: t.policy,
    label: ruleLabel(t.policy, source.policies),
    isPick: t.policy === pick,
    tried: t.tried,
    best: settingText(t.chosen),
    spread: t.spread ?? 0,
    touchy: !!t.touchy,
    pill: (t.touchy ? "touchy" : "steady") as TuningRow["pill"],
  }));
  return rows.sort(
    (a, b) =>
      Number(b.isPick) - Number(a.isPick) ||
      rankOf(a.policy) - rankOf(b.policy),
  );
}
