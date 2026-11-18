// Pure words and numbers for the run viewer (DESIGN.md section 4, words in 8.5).
import type { RunCentre, RunLever, RunOrder, RunViewModel } from "../types";

export type TimeUnit = RunViewModel["about"]["time_unit"];

const SHORT: Record<TimeUnit, string> = {
  minutes: "min",
  hours: "h",
  days: "days",
};

export function unitShort(unit: TimeUnit): string {
  return SHORT[unit];
}

export function displayId(id: string): string {
  return /^[a-z]{1,4}-?\d+$/.test(id) ? id.toUpperCase() : id;
}

export function pct(share: number, digits?: number): string {
  const d = digits ?? (share < 0.1 ? 1 : 0);
  return `${(share * 100).toFixed(d)}%`;
}

export function num(value: number, digits = 1): string {
  return value.toFixed(digits);
}

/** A signed time with a real minus sign: "+0.9 min", "−1.1 min". */
export function signedTime(value: number, unit: TimeUnit): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${Math.abs(value).toFixed(1)} ${SHORT[unit]}`;
}

export function joinAnd(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

export function pillText(order: RunOrder): string {
  const chance = order.late_chance;
  const share = chance == null ? "" : ` · ${Math.round(chance * 100)}%`;
  if (order.status === "unfinished") return "Not done";
  if (order.status === "late") return `Late${share}`;
  if (order.status === "risk") return `At risk${share}`;
  return chance != null && chance > 0 ? `OK${share}` : "On time";
}

/** Round ticks from 0 that stay inside the horizon, four or five of them. */
export function axisTicks(horizon: number): number[] {
  if (!(horizon > 0)) return [0];
  const power = 10 ** Math.floor(Math.log10(horizon / 4));
  const steps = [1, 2, 5, 10].map((m) => m * power);
  const count = (s: number) => Math.floor(horizon / s + 1e-9) + 1;
  const step = steps.reduce((a, b) =>
    Math.abs(count(b) - 4.5) < Math.abs(count(a) - 4.5) ? b : a,
  );
  return Array.from({ length: count(step) }, (_, i) =>
    Number((i * step).toFixed(6)),
  );
}

/** "Shaft mill" in the shaft row reads "Mill". */
export function stepName(label: string, part: string): string {
  const words = label.split(" ");
  if (words.length > 1 && words[0].toLowerCase() === part.toLowerCase()) {
    const rest = words.slice(1).join(" ");
    return rest[0].toUpperCase() + rest.slice(1);
  }
  return label;
}

export function lower(label: string): string {
  return label.slice(0, 2) === label.slice(0, 2).toUpperCase()
    ? label
    : label[0].toLowerCase() + label.slice(1);
}

export function plural(word: string, n: number): string {
  return n === 1 ? word : `${word}s`;
}

export function sortOrders(orders: RunOrder[]): RunOrder[] {
  return [...orders].sort((a, b) => {
    const ua = a.status === "unfinished" ? 0 : 1;
    const ub = b.status === "unfinished" ? 0 : 1;
    if (ua !== ub) return ua - ub;
    const ca = a.late_chance ?? -1;
    const cb = b.late_chance ?? -1;
    if (ca !== cb) return cb - ca;
    return (a.room ?? Infinity) - (b.room ?? Infinity);
  });
}

export type Figure = { value: string; label: string };

export function heroFigures(model: RunViewModel): Figure[] {
  const { answer, about } = model;
  const on = answer.on_time;
  const figures: Figure[] = [];
  if (on.mean != null) {
    const range =
      about.reps > 1 && on.low != null && on.high != null
        ? `of orders on time. Likely ${pct(on.low)} to ${pct(on.high)}.`
        : "of orders on time in this one test run.";
    figures.push({ value: pct(on.mean), label: range });
  }
  figures.push({
    value: num(answer.late_orders_per_run),
    label:
      about.reps > 1
        ? "late orders in a normal run"
        : "late orders in this run",
  });
  if (answer.choke) {
    figures.push({
      value: pct(answer.choke.busy),
      label: `how busy the ${lower(answer.choke.label)} is`,
    });
  } else {
    const top = [...model.centres].sort(
      (a, b) => (b.busy.mean ?? 0) - (a.busy.mean ?? 0),
    )[0];
    if (top)
      figures.push({
        value: pct(top.busy.mean ?? 0),
        label: `how busy the busiest step, the ${lower(top.label)}, is`,
      });
  }
  return figures;
}

export type SideRow = {
  key: string;
  value: string;
  from?: string;
  delta?: string;
  why?: string;
  warn?: boolean;
  command?: string;
};

function bestLever(levers: RunLever[]): RunLever | undefined {
  const best = levers.find((l) => l.tested);
  return best && (best.gain_points ?? 0) > 0 ? best : undefined;
}

function chanceWords(share: number): string {
  if (share >= 0.5) return "more than an even chance";
  if (share > 0.25) return "more than a 1 in 4 chance";
  if (share >= 0.2) return "at least a 1 in 5 chance";
  return "some chance";
}

function idleSteps(centres: RunCentre[], routes: RunViewModel["routes"]) {
  const idle = centres.filter(
    (c) => c.status === "ok" && (c.busy.mean ?? 0) < 0.1,
  );
  const partOf = new Map<string, string>();
  for (const r of routes) for (const s of r.steps) partOf.set(s, r.part);
  const names = [
    ...new Set(
      idle.map((c) => lower(stepName(c.label, partOf.get(c.id) ?? ""))),
    ),
  ];
  const top = Math.max(0, ...idle.map((c) => c.busy.mean ?? 0));
  return { idle, names, top };
}

export function sideRows(model: RunViewModel): SideRow[] {
  const { answer, about, orders, levers } = model;
  const rows: SideRow[] = [];
  const best = bestLever(levers);
  if (best && best.on_time != null && answer.on_time.mean != null) {
    const runs = model.sweep?.reps ?? about.reps;
    rows.push({
      key: best.label,
      from: `${pct(answer.on_time.mean)} →`,
      value: `${pct(best.on_time)} on time`,
      delta: `+${Math.round(best.gain_points ?? 0)} points`,
      why: [`Tested on the same ${runs} runs.`, best.note]
        .filter(Boolean)
        .join(" "),
    });
  } else {
    const idea = levers.find((l) => !l.tested && l.command);
    rows.push({
      key: "The one change that helps most",
      value: "No fixes tested yet",
      why: idea
        ? `Test one, for example: ${lower(idea.label)}.`
        : "Run a sweep with twinflow balance to test a change.",
      command: idea?.command,
    });
  }
  const watch = answer.watch
    .map((id) => orders.find((o) => o.id === id))
    .filter((o): o is RunOrder => o != null);
  if (watch.length) {
    const parts = [...new Set(watch.map((o) => o.part))];
    const chances = watch
      .map((o) => o.late_chance)
      .filter((c): c is number => c != null);
    const least = chances.length ? Math.min(...chances) : null;
    rows.push({
      key: "Orders at risk today",
      value: `${watch.length} of ${orders.length}`,
      delta:
        parts.length === 1
          ? `${watch.length > 1 ? "all " : ""}${plural(parts[0], watch.length)}`
          : `${parts.length} parts`,
      why:
        joinAnd(watch.map((o) => displayId(o.id))) +
        (least != null
          ? `. ${watch.length > 1 ? "Each has" : "It has"} ${chanceWords(least)} of being late.`
          : "."),
      warn: true,
    });
  } else if (orders.length) {
    rows.push({
      key: "Orders at risk today",
      value: `0 of ${orders.length}`,
      delta: "all have room",
    });
  }
  const { names, top } = idleSteps(model.centres, model.routes);
  if (names.length) {
    const cap = names[0][0].toUpperCase() + names[0].slice(1);
    rows.push({
      key: "Everything else",
      value: "Has room",
      delta: `under ${Math.max(1, Math.ceil(top * 100))}% busy`,
      why: `${joinAnd([cap, ...names.slice(1)])} steps sit idle most of the time. Do not add capacity there.`,
    });
  }
  return rows;
}

export function headlineParts(headline: string): [string, string] {
  // "Most will. Watch WO-9." -> lead "Most will." and accent "Watch WO-9."
  const cut = headline.indexOf(". ");
  return cut > 0
    ? [headline.slice(0, cut + 1), headline.slice(cut + 2)]
    : [headline, ""];
}
