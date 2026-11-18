import { describe, expect, it } from "vitest";
import { normalDay, pct, usd } from "../../src/decision/stateFormat";

describe("state map formatters", () => {
  it("formats dollars with separators and a real minus sign", () => {
    expect(usd(1383.4)).toBe("$1,383");
    expect(usd(-51)).toBe("−$51");
    expect(usd(0)).toBe("$0");
  });

  it("formats shares as percents", () => {
    expect(pct(0.0854)).toBe("8.5%");
    expect(pct(0.06)).toBe("6%");
    expect(pct(0.722, 0)).toBe("72%");
    expect(pct(0.755, 0)).toBe("76%");
  });

  it("spreads a run's profit over its days", () => {
    expect(Math.round(normalDay(65410, 60))).toBe(1090);
    expect(normalDay(100, 0)).toBe(0);
  });
});
