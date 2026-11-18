import { describe, expect, it } from "vitest";
import { readParams, writeParam } from "../../src/decision/viewState";

describe("view state in the URL", () => {
  it("reads hash and query strings the same way", () => {
    expect(readParams("#tab=replay&day=9")).toEqual({ tab: "replay", day: "9" });
    expect(readParams("?page=race&a=x")).toEqual({ page: "race", a: "x" });
    expect(readParams("")).toEqual({});
  });
  it("writes one key and keeps the others", () => {
    expect(writeParam("#a=1&b=2", "b", "3", "#")).toBe("#a=1&b=3");
    expect(writeParam("", "day", "9", "#")).toBe("#day=9");
    expect(writeParam("?page=race", "a", "x y", "?")).toBe("?page=race&a=x+y");
  });
  it("drops a key set back to its default (null)", () => {
    expect(writeParam("#a=1&b=2", "a", null, "#")).toBe("#b=2");
    expect(writeParam("#a=1", "a", null, "#")).toBe("");
  });
});
