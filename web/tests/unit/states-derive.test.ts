import { describe, expect, it } from "vitest";
import {
  lanesOf,
  moneyRows,
  visibleEdges,
  visibleNodes,
} from "../../src/decision/states/derive";
import type {
  StatesMarkovView,
  StatesStateNode,
} from "../../src/decision/types";

const node = (
  id: string,
  share: number,
  levels: Record<string, string> | null,
  setup: string | null,
): StatesStateNode =>
  ({
    id,
    label: id,
    parts_of_label: levels ? { levels, setup } : null,
    words: {
      title: id,
      setup: setup ? `Press on ${setup}` : "",
    },
    share,
    visits: 10,
    sticky: false,
    stay: 0.5,
    per_day: 100,
    money_per_day: {},
  }) as StatesStateNode;

describe("lanesOf", () => {
  it("makes one lane per press setting, biggest first, boxes by share", () => {
    const nodes = [
      node("a", 0.3, { grey: "low" }, "grey"),
      node("b", 0.4, { grey: "ok" }, "black"),
      node("c", 0.2, { grey: "high" }, "grey"),
    ];
    const out = lanesOf(nodes);
    expect(out.kind).toBe("setup");
    expect(out.lanes.map((l) => l.name)).toEqual([
      "Press on grey",
      "Press on black",
    ]);
    expect(out.lanes[0].nodes.map((n) => n.id)).toEqual(["a", "c"]);
  });

  it("falls back to the first stock level when there is no setup part", () => {
    const nodes = [
      node("a", 0.5, { grey: "low", black: "ok" }, null),
      node("b", 0.3, { grey: "ok", black: "ok" }, null),
      node("c", 0.1, { grey: "low", black: "high" }, null),
    ];
    const out = lanesOf(nodes);
    expect(out.kind).toBe("level");
    expect(out.lanes.map((l) => l.name)).toEqual(["Grey low", "Grey ok"]);
  });

  it("uses the grid when the view is not grouped", () => {
    const nodes = [node("a", 0.5, { round_bar: "0" }, null)];
    expect(lanesOf(nodes, false).kind).toBe("grid");
  });

  it("uses one grid sorted by share when labels have no parts", () => {
    const out = lanesOf([
      node("a", 0.1, null, null),
      node("b", 0.4, null, null),
    ]);
    expect(out.kind).toBe("grid");
    expect(out.lanes).toHaveLength(1);
    expect(out.lanes[0].nodes.map((n) => n.id)).toEqual(["b", "a"]);
  });
});

describe("visibleNodes and visibleEdges", () => {
  const chain: StatesMarkovView = {
    view: "family",
    moves: 100,
    coverage: 0.9,
    nodes: [
      node("s0", 0.5, null, null),
      node("s1", 0.3, null, null),
      node("s2", 0.1, null, null),
    ],
    edges: [
      { from: "s0", to: "s1", prob: 0.6, self: false },
      { from: "s0", to: "s0", prob: 0.3, self: true },
      { from: "s1", to: "s2", prob: 0.06, self: false },
    ],
  };

  it("keeps the first N nodes", () => {
    expect(visibleNodes(chain, 2).map((n) => n.id)).toEqual(["s0", "s1"]);
  });

  it("drops arrows to hidden states and arrows under the cut-off", () => {
    const ids = new Set(["s0", "s1"]);
    expect(visibleEdges(chain, ids, 0.05)).toHaveLength(2);
    expect(visibleEdges(chain, ids, 0.5).map((e) => e.to)).toEqual(["s1"]);
  });
});

describe("moneyRows", () => {
  it("names each part in plain words and scales the bars", () => {
    const rows = moneyRows({ revenue: 1000, setup: 250, lost_sales: 0 });
    expect(rows.map((r) => r.label)).toEqual([
      "sales",
      "changeovers",
      "lost sales",
    ]);
    expect(rows[0].width).toBe(1);
    expect(rows[1].width).toBeCloseTo(0.25);
  });
});
