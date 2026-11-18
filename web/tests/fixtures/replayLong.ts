// A made-up long run for the replay merge rule (above 180 days, cells merge to weeks).
// Deterministic: the press makes each family for 5 days, then rests one day.
import type { Day, ExampleRun, Product } from "../../src/decision/types";

export function longDays(n: number, products: Product[]): Day[] {
  const families = [...new Set(products.map((p) => p.family ?? ""))];
  const days: Day[] = [];
  let cumulative = 0;
  let last: string | null = null;
  for (let i = 0; i < n; i++) {
    const rest = i % 6 === 5;
    const family = rest ? null : families[Math.floor(i / 6) % families.length];
    const action = rest
      ? null
      : (products.find((p) => p.family === family)?.id ?? null);
    const cross = !rest && last !== null && family !== last;
    if (family) last = family;
    const profit = rest ? -150 : cross ? -400 : 700;
    cumulative += profit;
    const stock: Record<string, number> = {};
    products.forEach((p, k) => {
      stock[p.id] = ((i * 7 + k * 13) % 23) * (p.demand_rate ?? 1);
    });
    days.push({
      day: i + 1,
      action,
      setup: action,
      family,
      changeover: cross,
      cross_family: cross,
      stock,
      parts: { setup: cross ? 1200 : 0 },
      profit,
      cumulative,
    });
  }
  return days;
}

export function longRun(n: number, products: Product[]): ExampleRun {
  return {
    episode: 0,
    why: "typical",
    start_stock: {},
    days: longDays(n, products),
  };
}
