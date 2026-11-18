import { describe, expect, it } from "vitest";
import { leakTry, leakWords } from "../../src/decision/LeakList";
import { partsFromLabel } from "../../src/decision/stateMap";

describe("partsFromLabel", () => {
  it("reads the stock levels and the setup from a state label", () => {
    expect(partsFromLabel("grey:ok black:high natural:high | natural")).toEqual(
      {
        levels: { grey: "ok", black: "high", natural: "high" },
        setup: "natural",
      },
    );
    expect(partsFromLabel("a:low b:ok")).toEqual({
      levels: { a: "low", b: "ok" },
      setup: null,
    });
  });

  it("gives null for a label that is not family:level pairs", () => {
    expect(partsFromLabel("m1 busy")).toBeNull();
    expect(partsFromLabel("")).toBeNull();
  });
});

describe("leak words", () => {
  const leak = {
    state: "grey:high black:low natural:low | black",
    state_id: "s6",
    share: 0.0575,
    per_run: 2954,
    parts: { setup: 690, holding: 1108, lost_sales: 1156, late: 0 },
    cause: "lost_sales",
    cause_words: "lost sales",
  };

  it("names the state, its share and its biggest cost", () => {
    expect(leakWords(leak)).toEqual({
      name: "Grey high, black and natural low. Press on black.",
      detail: "5.8% of days. Biggest cost: lost sales.",
    });
  });

  it("prints the words Python sent when it sent some", () => {
    const words = { title: "Grey runs out", setup: "Press on black" };
    expect(leakWords({ ...leak, words }).name).toBe(
      "Grey runs out. Press on black.",
    );
  });

  it("uses the try line from the data, else a template keyed on cause", () => {
    expect(leakTry({ ...leak, try: "Try this." })).toBe("Try this.");
    expect(leakTry(leak)).toBe(
      "Try starting this colour sooner so it does not run out.",
    );
    expect(leakTry({ ...leak, cause: "setup" })).toBe(
      "Try a longer campaign so the press switches less here.",
    );
    expect(leakTry({ ...leak, cause: "holding" })).toBe(
      "Try a lower stock target so less sits on the shelf.",
    );
    expect(leakTry({ ...leak, cause: "other" })).toBe("");
  });
});
