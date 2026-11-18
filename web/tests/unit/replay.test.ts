import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type {
  ExampleRun,
  Product,
  RaceViewModel,
} from "../../src/decision/types";
import {
  cellLevel,
  colourChanges,
  dayBuckets,
  daysOfStock,
  outProductDays,
  outProducts,
  replayRuns,
  stockLevel,
  stockRows,
} from "../../src/decision/replay";
import { longDays } from "../fixtures/replayLong";

const FULL = fileURLToPath(
  new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
);
const model = JSON.parse(readFileSync(FULL, "utf8")) as RaceViewModel;
const today = model.example_runs.lowest_cover as ExampleRun;
const pick = model.example_runs.family_campaign as ExampleRun;
const products = model.products as Product[];

describe("replay strips data (B-18)", () => {
  it("counts 41 colour changes today and 9 with the pick", () => {
    expect(colourChanges(today)).toBe(41);
    expect(colourChanges(pick)).toBe(9);
  });

  it("counts 124 and 53 product-days out of stock (under 1 day of stock)", () => {
    expect(outProductDays(today, products)).toBe(124);
    expect(outProductDays(pick, products)).toBe(53);
  });

  it("reads days of stock and the four stock levels", () => {
    expect(daysOfStock(0, 9)).toBe(0);
    expect(daysOfStock(90, 9)).toBe(10);
    expect(daysOfStock(5, 0)).toBe(Infinity);
    expect(stockLevel(0.5)).toBe("out");
    expect(stockLevel(3.9)).toBe("low");
    expect(stockLevel(4)).toBe("mid");
    expect(stockLevel(10)).toBe("mid");
    expect(stockLevel(10.1)).toBe("high");
  });

  it("names the products out on day 9 of the pick", () => {
    expect(outProducts(pick.days[8], products)).toEqual([
      "BLK-S",
      "BLK-L",
      "BLK-XL",
      "NAT-S",
      "NAT-M",
      "NAT-L",
      "NAT-XL",
    ]);
  });

  it("keeps one cell per day up to 180 days", () => {
    const cells = dayBuckets(today.days);
    expect(cells).toHaveLength(60);
    expect(cells[8]).toMatchObject({ start: 9, end: 9 });
  });

  it("merges a 200-day run into week cells", () => {
    const cells = dayBuckets(longDays(200, products));
    expect(cells).toHaveLength(29);
    expect(cells[0]).toMatchObject({ start: 1, end: 7 });
    expect(cells[28]).toMatchObject({ start: 197, end: 200 });
    expect(cells[1].changes).toBe(
      cells[1].days.filter((d) => d.cross_family).length,
    );
  });

  it("keeps one stock row per product up to 30 products", () => {
    const rows = stockRows(products, model.families, new Set());
    expect(rows.map((r) => r.label)).toEqual(products.map((p) => p.id));
    expect(rows.every((r) => !r.group)).toBe(true);
  });

  it("groups more than 30 products by family and opens one on request", () => {
    const many: Product[] = Array.from({ length: 36 }, (_, i) => ({
      id: `P${i}`,
      family: ["grey", "black", "natural"][i % 3],
      demand_rate: 10,
    }));
    const closed = stockRows(many, ["grey", "black", "natural"], new Set());
    expect(closed.map((r) => r.label)).toEqual(["grey", "black", "natural"]);
    expect(closed[0]).toMatchObject({ group: true, open: false });
    expect(closed[0].products).toHaveLength(12);
    const open = stockRows(
      many,
      ["grey", "black", "natural"],
      new Set(["black"]),
    );
    expect(open).toHaveLength(3 + 12);
    expect(open[1]).toMatchObject({ label: "black", group: true, open: true });
    expect(open[2]).toMatchObject({
      label: "P1",
      group: false,
      family: "black",
    });
  });

  it("colours a family row by its worst product", () => {
    const cell = dayBuckets(pick.days)[8];
    const black = stockRows(products, model.families, new Set()).filter(
      (r) => r.family === "black",
    );
    expect(cellLevel(cell, black[0], products)).toBe("out");
    expect(
      cellLevel(
        cell,
        {
          key: "black",
          label: "black",
          family: "black",
          group: true,
          open: false,
          products: ["BLK-S", "BLK-M"],
        },
        products,
      ),
    ).toBe("out");
  });

  it("puts today first and the pick second", () => {
    const runs = replayRuns(model);
    expect(runs.map((r) => [r.id, r.tag])).toEqual([
      ["lowest_cover", "today"],
      ["family_campaign", "pick"],
    ]);
    expect(runs[0].label).toBe("Lowest cover");
  });
});
