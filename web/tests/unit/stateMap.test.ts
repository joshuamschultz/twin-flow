import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  layoutStates,
  levelInfo,
  moveSelection,
  nodeWords,
  stateWords,
  type StateLayout,
} from "../../src/decision/stateMap";
import type { RaceViewModel, StateNode } from "../../src/decision/types";

const GOLDEN = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;
const LEVELS = GOLDEN.views.levels!;
const PICK = GOLDEN.markov[GOLDEN.recommendation.policy];
const TODAY = GOLDEN.markov[GOLDEN.baseline.policy];

function parts(label: string): StateNode["parts_of_label"] {
  const [stock, setup] = label.split(" | ");
  const levels = Object.fromEntries(stock.split(" ").map((t) => t.split(":")));
  return { levels, setup: setup ?? null };
}

function node(id: string, share: number, label: string, withParts = true) {
  return {
    id,
    label,
    parts_of_label: withParts ? parts(label) : null,
    share,
    visits: 1,
    stay: 0.5,
    per_day: 100,
    money_per_day: {},
  } satisfies StateNode;
}

function overlaps(layout: StateLayout): string[] {
  const boxes = Object.entries(layout.boxes);
  const hits: string[] = [];
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const [a, p] = boxes[i];
      const [b, q] = boxes[j];
      const apart =
        p.x + p.w <= q.x ||
        q.x + q.w <= p.x ||
        p.y + p.h <= q.y ||
        q.y + q.h <= p.y;
      if (!apart) hits.push(`${a}/${b}`);
    }
  return hits;
}

describe("stateWords", () => {
  it("names the families that are not at the normal level, high first", () => {
    expect(
      stateWords(parts("grey:high black:ok natural:low | black"), LEVELS),
    ).toEqual({ title: "Grey high, natural low", setup: "Press on black" });
  });

  it("joins families that share a level with 'and'", () => {
    expect(
      stateWords(parts("grey:high black:low natural:low | grey"), LEVELS).title,
    ).toBe("Grey high, black and natural low");
    expect(
      stateWords(parts("grey:low black:low natural:ok | grey"), LEVELS).title,
    ).toBe("Grey and black low");
  });

  it("says 'All stock' when every family is at one level", () => {
    expect(
      stateWords(parts("grey:ok black:ok natural:ok | grey"), LEVELS),
    ).toEqual({ title: "All stock OK", setup: "Press on grey" });
    expect(
      stateWords(parts("grey:low black:low natural:low | grey"), LEVELS).title,
    ).toBe("All stock low");
    expect(
      stateWords(parts("grey:high black:high natural:high | grey"), LEVELS)
        .title,
    ).toBe("All stock high");
  });

  it("works without a levels list and without a setup", () => {
    expect(stateWords(parts("a:low b:ok"))).toEqual({
      title: "A low",
      setup: "",
    });
  });

  it("gives empty words for a state with no parts", () => {
    expect(stateWords(null)).toEqual({ title: "", setup: "" });
  });

  it("has no em dash in any title", () => {
    for (const chain of Object.values(GOLDEN.markov))
      for (const n of chain.nodes) {
        const w = nodeWords(n, LEVELS);
        expect(`${w.title} ${w.setup}`).not.toMatch(/—/);
      }
  });
});

describe("nodeWords", () => {
  it("builds words from the parts when there are parts", () => {
    expect(nodeWords(PICK.nodes[0], LEVELS)).toEqual({
      title: "All stock OK",
      setup: "Press on grey",
    });
  });

  it("falls back to the given words, then to the raw label", () => {
    const n = node("x", 0.1, "station:busy", false);
    expect(nodeWords(n)).toEqual({ title: "station:busy", setup: "" });
    expect(nodeWords({ ...n, words: { title: "Busy", setup: "" } })).toEqual({
      title: "Busy",
      setup: "",
    });
  });
});

describe("levelInfo", () => {
  it("ranks levels in order and marks the lowest one as low", () => {
    expect(levelInfo("low", LEVELS)).toEqual({
      index: 0,
      count: 3,
      low: true,
    });
    expect(levelInfo("high", LEVELS)).toEqual({
      index: 2,
      count: 3,
      low: false,
    });
  });
});

describe("layoutStates", () => {
  it("puts the golden pick chain in three lanes of 6, 6 and 3 sorted by share", () => {
    const layout = layoutStates(PICK.nodes, PICK.edges, "auto", {
      width: 900,
      order: GOLDEN.families,
    });
    expect(layout.mode).toBe("setup");
    expect(layout.lanes.map((l) => l.key)).toEqual([
      "grey",
      "black",
      "natural",
    ]);
    expect(layout.lanes.map((l) => l.title)).toEqual([
      "Press on grey",
      "Press on black",
      "Press on natural",
    ]);
    expect(layout.lanes.map((l) => l.ids.length)).toEqual([6, 6, 3]);
    const share = Object.fromEntries(PICK.nodes.map((n) => [n.id, n.share]));
    for (const lane of layout.lanes) {
      const shares = lane.ids.map((id) => share[id]);
      expect(shares).toEqual([...shares].sort((a, b) => b - a));
    }
  });

  it("never lets two boxes overlap, at wide and narrow widths", () => {
    for (const chain of Object.values(GOLDEN.markov))
      for (const width of [1200, 740, 320]) {
        const layout = layoutStates(chain.nodes, chain.edges, "auto", {
          width,
          order: GOLDEN.families,
        });
        expect(overlaps(layout)).toEqual([]);
        expect(Object.keys(layout.boxes)).toHaveLength(chain.nodes.length);
      }
  });

  it("is the same every time (no random layout)", () => {
    const a = layoutStates(TODAY.nodes, TODAY.edges, "auto", { width: 800 });
    const b = layoutStates(TODAY.nodes, TODAY.edges, "auto", { width: 800 });
    expect(a).toEqual(b);
  });

  it("falls back to lanes by the first stock level when there is no setup", () => {
    const nodes = [
      node("a", 0.3, "grey:low black:ok"),
      node("b", 0.2, "grey:high black:low"),
      node("c", 0.4, "grey:low black:high"),
    ];
    const layout = layoutStates(nodes, [], "auto", { levels: LEVELS });
    expect(layout.mode).toBe("level");
    expect(layout.lanes.map((l) => [l.title, l.ids])).toEqual([
      ["Grey low", ["c", "a"]],
      ["Grey high", ["b"]],
    ]);
  });

  it("uses one grid sorted by share for a chain with no parts_of_label", () => {
    const nodes = [
      node("a", 0.1, "m1:busy m2:idle", false),
      node("b", 0.5, "m1:idle m2:idle", false),
      node("c", 0.3, "m1:busy m2:busy", false),
      node("d", 0.05, "m1:down m2:idle", false),
    ];
    const layout = layoutStates(nodes, [], "auto", { width: 520 });
    expect(layout.mode).toBe("grid");
    expect(layout.lanes).toEqual([]);
    expect(layout.order).toEqual(["b", "c", "a", "d"]);
    expect(layout.columns).toBe(2);
    expect(layout.boxes.b.y).toBe(layout.boxes.c.y);
    expect(layout.boxes.a.y).toBeGreaterThan(layout.boxes.b.y);
    expect(overlaps(layout)).toEqual([]);
  });

  it("draws an arrow for every move between two boxes on the map, not self moves", () => {
    const layout = layoutStates(PICK.nodes, PICK.edges, "auto", {
      width: 900,
    });
    const onMap = PICK.edges.filter((e) => !e.self && e.from !== e.to);
    expect(layout.edges).toHaveLength(onMap.length);
    for (const e of layout.edges) expect(e.d).toMatch(/^M[\d.]+ [\d.]+ C/);
  });
});

describe("moveSelection", () => {
  const layout = layoutStates(PICK.nodes, PICK.edges, "auto", {
    width: 900,
    order: GOLDEN.families,
  });
  const [grey, black, natural] = layout.lanes.map((l) => l.ids);

  it("moves up and down inside a lane and stops at the ends", () => {
    expect(moveSelection(layout, grey[0], "ArrowDown")).toBe(grey[1]);
    expect(moveSelection(layout, grey[1], "ArrowUp")).toBe(grey[0]);
    expect(moveSelection(layout, grey[0], "ArrowUp")).toBe(grey[0]);
    expect(moveSelection(layout, grey[5], "ArrowDown")).toBe(grey[5]);
  });

  it("moves across lanes at the same row, or the last box of a shorter lane", () => {
    expect(moveSelection(layout, grey[2], "ArrowRight")).toBe(black[2]);
    expect(moveSelection(layout, black[5], "ArrowRight")).toBe(natural[2]);
    expect(moveSelection(layout, natural[0], "ArrowLeft")).toBe(black[0]);
    expect(moveSelection(layout, grey[0], "ArrowLeft")).toBe(grey[0]);
  });

  it("goes to the first and last box with Home and End", () => {
    expect(moveSelection(layout, black[3], "Home")).toBe(grey[0]);
    expect(moveSelection(layout, black[3], "End")).toBe(natural[2]);
  });

  it("walks the grid in reading order", () => {
    const nodes = ["a", "b", "c", "d", "e"].map((id, i) =>
      node(id, 0.5 - i * 0.1, `x:${id}`, false),
    );
    const grid = layoutStates(nodes, [], "auto", { width: 520 });
    expect(moveSelection(grid, "a", "ArrowRight")).toBe("b");
    expect(moveSelection(grid, "a", "ArrowDown")).toBe("c");
    expect(moveSelection(grid, "d", "ArrowUp")).toBe("b");
    expect(moveSelection(grid, "d", "ArrowDown")).toBe("d");
  });
});
