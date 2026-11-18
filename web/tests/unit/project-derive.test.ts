import { describe, expect, it } from "vitest";
import { ruleCounts, viewsByKind } from "../../src/decision/project/derive";

const rule = (policy: string, source: "project" | "builtin", base = false) => ({
  policy,
  label: policy,
  about: "",
  source,
  file: source === "project" ? `policies/${policy}.py` : null,
  is_baseline: base,
  settings_tried: 1,
  ok: true,
});

describe("ruleCounts", () => {
  it("counts own and built-in rules and names the one you run today", () => {
    const out = ruleCounts([
      rule("a", "project"),
      rule("b", "project"),
      rule("c", "builtin", true),
      rule("d", "builtin"),
    ]);
    expect(out).toEqual({ own: 2, builtIn: 2, today: "c" });
  });

  it("has no 'today' when no rule is the baseline", () => {
    expect(ruleCounts([rule("a", "builtin")]).today).toBeNull();
  });
});

describe("viewsByKind", () => {
  it("splits state views from observation levels", () => {
    const out = viewsByKind([
      { name: "family", kind: "state_view", about: "" },
      { name: "full", kind: "observation", about: "" },
    ]);
    expect(out.stateViews.map((v) => v.name)).toEqual(["family"]);
    expect(out.observations.map((v) => v.name)).toEqual(["full"]);
  });
});
