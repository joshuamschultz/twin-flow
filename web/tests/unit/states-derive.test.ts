import { describe, expect, it } from "vitest";
import {
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
