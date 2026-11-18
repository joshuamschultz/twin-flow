// Pure helpers for the floor replay (DESIGN.md 3.7). No React here, so vitest can cover them.
import { usd } from "./format";
import type {
  Day,
  Economics,
  ExampleRun,
  Product,
  RaceViewModel,
} from "./types";

export type StockLevel = "out" | "low" | "mid" | "high";
export type ChangeKind = "colour" | "size" | "none";
export type LaneTag = "today" | "pick" | "plain";

/** Above this many days, cells merge to weeks. */
export const MAX_DAY_CELLS = 180;
/** Above this many products, the stock strip groups rows by family. */
export const MAX_PRODUCT_ROWS = 30;

export function daysOfStock(
  units: number,
  rate: number | null | undefined,
): number {
  if (!rate || rate <= 0) return units > 0 ? Infinity : 0;
  return units / rate;
}

/** Under 1 day is out of stock. Then under 4, 4 to 10, over 10 (DESIGN.md 3.7). */
export function stockLevel(days: number): StockLevel {
  if (days < 1) return "out";
  if (days < 4) return "low";
  if (days <= 10) return "mid";
  return "high";
}

const LEVEL_ORDER: StockLevel[] = ["out", "low", "mid", "high"];

function worst(a: StockLevel, b: StockLevel): StockLevel {
  return LEVEL_ORDER.indexOf(a) <= LEVEL_ORDER.indexOf(b) ? a : b;
}

export function isRest(day: Day): boolean {
  return day.family === null && (day.action === null || day.action === "idle");
}

export function changeKind(day: Day): ChangeKind {
  if (day.cross_family) return "colour";
  if (day.changeover) return "size";
  return "none";
}

export function colourChanges(run: ExampleRun): number {
  return run.days.filter((d) => d.cross_family).length;
}

export function changeovers(days: Day[]): number {
  return days.filter((d) => d.changeover || d.cross_family).length;
}

function rates(products: Product[]): Map<string, number | null | undefined> {
  return new Map(products.map((p) => [p.id, p.demand_rate]));
}

export function outProducts(day: Day, products: Product[]): string[] {
  return products
    .filter((p) => daysOfStock(day.stock[p.id] ?? 0, p.demand_rate) < 1)
    .map((p) => p.id);
}

export function outProductDays(run: ExampleRun, products: Product[]): number {
  return run.days.reduce((n, d) => n + outProducts(d, products).length, 0);
}

// ---------- cells on the day axis ----------

export interface DayCell {
  /** First and last day number in the cell (equal when cells are days). */
  start: number;
  end: number;
  days: Day[];
  /** The family the press made most in the cell, or null if it rested the whole time. */
  family: string | null;
  /** Colour changes in the cell. */
  changes: number;
  /** Size changes in the cell. */
  sizes: number;
}

function mainFamily(days: Day[]): string | null {
  const count = new Map<string, number>();
  for (const d of days)
    if (d.family) count.set(d.family, (count.get(d.family) ?? 0) + 1);
  let best: string | null = null;
  let most = 0;
  for (const [f, n] of count) {
    if (n > most) {
      best = f;
      most = n;
    }
  }
  return best;
}

export function dayBuckets(days: Day[], maxDays = MAX_DAY_CELLS): DayCell[] {
  const size = days.length > maxDays ? 7 : 1;
  const cells: DayCell[] = [];
  for (let i = 0; i < days.length; i += size) {
    const part = days.slice(i, i + size);
    cells.push({
      start: part[0].day,
      end: part[part.length - 1].day,
      days: part,
      family: mainFamily(part),
      changes: part.filter((d) => d.cross_family).length,
      sizes: part.filter((d) => d.changeover && !d.cross_family).length,
    });
  }
  return cells;
}

export function cellIndexOfDay(cells: DayCell[], day: number): number {
  const i = cells.findIndex((c) => day >= c.start && day <= c.end);
  return i < 0 ? cells.length - 1 : i;
}

// ---------- stock rows ----------

export interface StockRow {
  key: string;
  label: string;
  family: string | null;
  /** A family row stands for its products. A product row has one product. */
  group: boolean;
  open: boolean;
  products: string[];
}

export function stockRows(
  products: Product[],
  families: string[] | null | undefined,
  open: Set<string>,
  maxRows = MAX_PRODUCT_ROWS,
): StockRow[] {
  const familyOrder = [
    ...(families ?? []),
    ...products
      .map((p) => p.family ?? "")
      .filter((f) => !(families ?? []).includes(f)),
  ].filter((f, i, all) => all.indexOf(f) === i);
  const byFamily = (f: string) =>
    products.filter((p) => (p.family ?? "") === f);
  const productRow = (p: Product): StockRow => ({
    key: p.id,
    label: p.id,
    family: p.family ?? null,
    group: false,
    open: false,
    products: [p.id],
  });
  if (products.length <= maxRows) {
    return familyOrder.flatMap((f) => byFamily(f).map(productRow));
  }
  return familyOrder.flatMap((f) => {
    const members = byFamily(f);
    const isOpen = open.has(f);
    const head: StockRow = {
      key: `family:${f}`,
      label: f || "other",
      family: f || null,
      group: true,
      open: isOpen,
      products: members.map((p) => p.id),
    };
    return isOpen ? [head, ...members.map(productRow)] : [head];
  });
}

/** The worst stock level of the row's products over the cell's days. */
export function cellLevel(
  cell: DayCell,
  row: StockRow,
  products: Product[],
): StockLevel {
  const rate = rates(products);
  let level: StockLevel = "high";
  for (const d of cell.days) {
    for (const id of row.products) {
      level = worst(
        level,
        stockLevel(daysOfStock(d.stock[id] ?? 0, rate.get(id))),
      );
      if (level === "out") return level;
    }
  }
  return level;
}

/** Days of stock of the row on one day (the lowest product for a family row). */
export function rowDaysOfStock(
  day: Day,
  row: StockRow,
  products: Product[],
): number {
  const rate = rates(products);
  return Math.min(
    ...row.products.map((id) => daysOfStock(day.stock[id] ?? 0, rate.get(id))),
  );
}

// ---------- words ----------

export function lostWords(share: number | null | undefined): string | null {
  if (!share || share <= 0) return null;
  const known: [number, string][] = [
    [1, "the whole day lost"],
    [0.75, "three quarters of the day lost"],
    [0.5, "half the day lost"],
    [0.25, "a quarter of the day lost"],
    [0.1, "a tenth of the day lost"],
  ];
  for (const [v, w] of known) if (Math.abs(share - v) < 1e-9) return w;
  return `${Math.round(share * 100)}% of the day lost`;
}

function changeCost(
  day: Day,
  kind: ChangeKind,
  economics: Economics | undefined,
): number | null {
  const fromDay = day.parts?.setup;
  if (typeof fromDay === "number" && fromDay > 0) return fromDay;
  const c = economics?.changeover;
  if (!c) return null;
  return (kind === "colour" ? c.across_family : c.within_family)?.cost ?? null;
}

function changeShare(
  kind: ChangeKind,
  economics: Economics | undefined,
): number | null {
  const c = economics?.changeover;
  if (!c) return null;
  return (
    (kind === "colour" ? c.across_family : c.within_family)?.output_lost ?? null
  );
}

/** "Colour change to black ($1,200, half the day lost)." */
export function changeSentence(day: Day, economics?: Economics): string {
  if (isRest(day)) return "The press rests.";
  const kind = changeKind(day);
  if (kind === "none") return "No changeover.";
  const cost = changeCost(day, kind, economics);
  const lost = lostWords(changeShare(kind, economics));
  const extra = [cost === null ? null : usd(cost), lost]
    .filter(Boolean)
    .join(", ");
  const head =
    kind === "colour" ? `Colour change to ${day.family}` : "Size change";
  return extra ? `${head} (${extra}).` : `${head}.`;
}

function listOut(ids: string[], limit = 8): string {
  if (ids.length <= limit) return ids.join(", ");
  return `${ids.slice(0, limit).join(", ")} and ${ids.length - limit} more`;
}

export interface DayFactsData {
  day: number;
  makes: string;
  changeoversSoFar: number;
  out: string[];
  outLabel: string;
  profitToday: number;
  profitSoFar: number;
  change: string;
  sentence: string;
}

export function dayFacts(
  run: ExampleRun,
  dayNumber: number,
  products: Product[],
  economics?: Economics,
): DayFactsData | null {
  const index = run.days.findIndex((d) => d.day === dayNumber);
  if (index < 0) return null;
  const day = run.days[index];
  const out = outProducts(day, products);
  const change = changeSentence(day, economics);
  const outText = out.length
    ? `Out of stock: ${listOut(out)}.`
    : "Every product is in stock.";
  return {
    day: day.day,
    makes: isRest(day) ? "Rests" : (day.action ?? day.family ?? "Rests"),
    changeoversSoFar: changeovers(run.days.slice(0, index + 1)),
    out,
    outLabel: `${out.length} of ${products.length}`,
    profitToday: day.profit,
    profitSoFar: day.cumulative,
    change,
    sentence: `Day ${day.day}. ${change} ${outText} Profit today: ${usd(day.profit)}.`,
  };
}

/** Hover words for one press cell. */
export function pressWords(cell: DayCell, economics?: Economics): string {
  if (cell.start !== cell.end) {
    const made = cell.family ? `Mostly ${cell.family}` : "The press rests";
    const ch =
      cell.changes === 1 ? "1 colour change" : `${cell.changes} colour changes`;
    return `Days ${cell.start} to ${cell.end}: ${made}. ${ch}.`;
  }
  const d = cell.days[0];
  if (isRest(d)) return `Day ${d.day}: the press rests.`;
  const kind = changeKind(d);
  const tail = kind === "none" ? "" : ` ${changeSentence(d, economics)}`;
  return `Day ${d.day}: makes ${d.action ?? d.family}.${tail}`;
}

export function stockWords(
  cell: DayCell,
  row: StockRow,
  products: Product[],
): string {
  const name = row.group ? `${row.label} (lowest product)` : row.label;
  if (cell.start !== cell.end) {
    const level = cellLevel(cell, row, products);
    const w =
      level === "out" ? "out of stock on some days" : "in stock every day";
    return `${name}, days ${cell.start} to ${cell.end}: ${w}.`;
  }
  const days = rowDaysOfStock(cell.days[0], row, products);
  if (days < 1) return `${name}, day ${cell.start}: out of stock.`;
  const n = Number.isFinite(days) ? Math.round(days) : "many";
  return `${name}, day ${cell.start}: ${n} days of stock.`;
}

// ---------- lanes from the view model ----------

export interface ReplayRun {
  id: string;
  label: string;
  tag: LaneTag;
  run: ExampleRun;
}

/** Today first, then the pick. One lane when they are the same rule. */
export function replayRuns(model: RaceViewModel): ReplayRun[] {
  const label = (id: string) =>
    model.policies.find((p) => p.policy === id)?.label ?? id;
  const ids: [string, LaneTag][] = [];
  const base = model.baseline?.policy;
  const pick = model.recommendation?.policy;
  if (base) ids.push([base, "today"]);
  if (pick && pick !== base) ids.push([pick, "pick"]);
  return ids
    .filter(([id]) => model.example_runs[id])
    .map(([id, tag]) => ({
      id,
      label: label(id),
      tag,
      run: model.example_runs[id],
    }));
}

/** The takeaway under both lanes (DESIGN.md 3.7). */
export function replayTakeaway(runs: ReplayRun[], products: Product[]): string {
  const today = runs.find((r) => r.tag === "today");
  const pick = runs.find((r) => r.tag === "pick");
  if (!today || !pick) {
    const only = runs[0];
    if (!only) return "";
    return `In this run, the rule changes colour ${colourChanges(only.run)} times. Product-days out of stock: ${outProductDays(only.run, products)}.`;
  }
  const pc = colourChanges(pick.run);
  const span = pick.run.days.length / (pc + 1);
  const runFor =
    span >= 5 && span <= 9 ? "about a week" : `about ${Math.round(span)} days`;
  return (
    `Same orders, two very different floors. In this run, today's rule changes colour ` +
    `${colourChanges(today.run)} times. The pick changes colour ${pc} times and runs each ` +
    `colour for ${runFor}. Product-days out of stock: ${outProductDays(today.run, products)} ` +
    `today, ${outProductDays(pick.run, products)} with the pick.`
  );
}

// ---------- controls ----------

/** The day a key moves to, or null when the key does nothing (DESIGN.md 8.4). */
export function keyDay(key: string, day: number, max: number): number | null {
  const clamp = (d: number) => Math.min(max, Math.max(1, d));
  switch (key) {
    case "ArrowRight":
    case "ArrowUp":
      return clamp(day + 1);
    case "ArrowLeft":
    case "ArrowDown":
      return clamp(day - 1);
    case "PageUp":
      return clamp(day + 10);
    case "PageDown":
      return clamp(day - 10);
    case "Home":
      return 1;
    case "End":
      return max;
    default:
      return null;
  }
}

/** Play moves one day at a time and stops at the last day. */
export function nextPlayDay(day: number, max: number): number | null {
  return day >= max ? null : day + 1;
}

/** About 3 days a second (DESIGN.md 8.3). */
export const PLAY_MS = 333;

/** The day from the URL value (`day=9`), clamped to the run. Bad values give day 1. */
export function parseDay(raw: string, max: number): number {
  if (!/^\d+$/.test(raw)) return 1;
  return Math.min(max, Math.max(1, Number(raw)));
}
