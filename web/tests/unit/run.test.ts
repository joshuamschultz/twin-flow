import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { RunViewModel } from "../../src/decision/types";
import {
  axisTicks,
  displayId,
  heroFigures,
  pct,
  pillText,
  sideRows,
  sortOrders,
  stepName,
} from "../../src/decision/run";

const golden = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL(
        "../../../docs/design/run-view-model.example.json",
        import.meta.url,
      ),
    ),
    "utf8",
  ),
) as RunViewModel;

describe("run viewer words", () => {
  it("shows order ids the way people write them", () => {
    expect(displayId("wo-9")).toBe("WO-9");
    expect(displayId("order_12")).toBe("order_12");
  });

  it("puts words and the chance on every pill", () => {
    const base = golden.orders[0];
    expect(pillText({ ...base, status: "late", late_chance: 0.567 })).toBe(
      "Late · 57%",
    );
    expect(pillText({ ...base, status: "risk", late_chance: 0.433 })).toBe(
      "At risk · 43%",
    );
    expect(pillText({ ...base, status: "ok", late_chance: 0.033 })).toBe(
      "OK · 3%",
    );
    expect(pillText({ ...base, status: "ok", late_chance: 0 })).toBe("On time");
    expect(pillText({ ...base, status: "ok", late_chance: null })).toBe(
      "On time",
    );
    expect(pillText({ ...base, status: "late", late_chance: null })).toBe(
      "Late",
    );
    expect(pillText({ ...base, status: "unfinished", late_chance: null })).toBe(
      "Not done",
    );
  });

  it("formats shares with one decimal under 10%", () => {
    expect(pct(0.873)).toBe("87%");
    expect(pct(0.015)).toBe("1.5%");
    expect(pct(0.987)).toBe("99%");
  });

  it("picks round axis ticks inside the horizon", () => {
    expect(axisTicks(62)).toEqual([0, 20, 40, 60]);
    expect(axisTicks(9)).toEqual([0, 2, 4, 6, 8]);
    expect(axisTicks(0)).toEqual([0]);
  });

  it("drops the part word from a step name in its own row", () => {
    expect(stepName("Shaft mill", "shaft")).toBe("Mill");
    expect(stepName("Bracket inspection", "bracket")).toBe("Inspection");
    expect(stepName("Oven", "bread")).toBe("Oven");
  });

  it("sorts orders by late chance, then by room", () => {
    const ids = sortOrders(golden.orders).map((o) => o.id);
    expect(ids.slice(0, 3)).toEqual(["wo-9", "wo-5", "wo-7"]);
  });

  it("builds the three hero figures", () => {
    expect(heroFigures(golden)).toEqual([
      { value: "87%", label: "of orders on time. Likely 83% to 91%." },
      { value: "1.3", label: "late orders in a normal run" },
      { value: "99%", label: "how busy the shaft mill is" },
    ]);
  });

  it("one test run has no range on the on-time figure", () => {
    const one = { ...golden, about: { ...golden.about, reps: 1 } };
    expect(heroFigures(one)[0].label).toBe(
      "of orders on time in this one test run.",
    );
  });

  it("builds the side card from the best change, the watch list and the rest", () => {
    const rows = sideRows(golden);
    expect(rows[0]).toMatchObject({
      key: "Add a second shaft mill",
      from: "87% →",
      value: "99% on time",
      delta: "+11 points",
    });
    expect(rows[0].why).toBe(
      "Tested on the same 30 runs. Late orders drop from 1.3 to 0.1 per run.",
    );
    expect(rows[1]).toMatchObject({
      key: "Orders at risk today",
      value: "3 of 10",
      delta: "all shafts",
      warn: true,
    });
    expect(rows[1].why).toBe(
      "WO-9, WO-5 and WO-7. Each has more than a 1 in 4 chance of being late.",
    );
    expect(rows[2]).toMatchObject({
      key: "Everything else",
      value: "Has room",
      delta: "under 2% busy",
    });
  });

  it("says no fix was tested yet and gives the command", () => {
    const none = {
      ...golden,
      levers: golden.levers.filter((l) => !l.tested),
      sweep: null,
    };
    const first = sideRows(none)[0];
    expect(first.key).toBe("The one change that helps most");
    expect(first.value).toBe("No fixes tested yet");
    expect(first.command).toMatch(/^twinflow balance /);
  });

  it("uses no em dash in any text", () => {
    const text = JSON.stringify([heroFigures(golden), sideRows(golden)]);
    expect(text).not.toMatch(/[–—]/);
  });
});
