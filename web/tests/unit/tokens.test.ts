import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { tokensText } from "../../scripts/tokens-lib.mjs";
import { contrast } from "./contrast";

const web = fileURLToPath(new URL("../../", import.meta.url));
const read = (f: string) => readFileSync(web + f, "utf8");
const json = JSON.parse(read("src/decision/tokens.json")) as {
  light: Record<string, string>;
  dark: Record<string, string>;
};

// DESIGN.md 8.1: every decision token, in both themes.
const TABLE = [
  "--tf-pick", "--tf-today", "--tf-gain", "--tf-gain-fill", "--tf-loss", "--tf-loss-text",
  "--tf-seq-1", "--tf-seq-2", "--tf-seq-3", "--tf-out", "--tf-fam-1", "--tf-fam-2",
  "--tf-fam-3", "--tf-grid", "--tf-axis", "--tf-hero-bg",
]; // prettier-ignore

describe("tokens", () => {
  it("has every token of the 8.1 table in both themes", () => {
    for (const name of TABLE) {
      expect(json.light[name], name).toMatch(/^#[0-9a-f]{6}$/i);
      expect(json.dark[name], name).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  it("tokens.json is fresh (run `npm run tokens`)", () => {
    const fresh = tokensText([
      read("src/index.css"),
      read("src/decision/tokens.css"),
    ]);
    expect(read("src/decision/tokens.json")).toBe(fresh);
  });

  for (const theme of ["light", "dark"] as const) {
    const t = json[theme];
    const surface = t["--ba-surface"];
    const page = t["--tf-page"];
    const hero = t["--tf-hero-bg"];

    it(`${theme}: text tokens are at least 4.5:1 on their surface`, () => {
      const pairs: [string, string][] = [
        ["--tf-gain", surface],
        ["--tf-loss-text", surface],
        ["--tf-meta", surface],
        ["--tf-meta", page],
        ["--ba-body", surface],
        ["--ba-body-2", surface],
        ["--ba-headline", surface],
        ["--tf-hero-ink", hero],
        ["--tf-hero-sub", hero],
        ["--tf-hero-muted", hero],
        ["--tf-hero-accent", hero],
        ["--tf-hero-warn", hero],
      ];
      for (const [name, ground] of pairs) {
        expect(
          contrast(t[name], ground),
          `${name} on ${ground}`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    });

    it(`${theme}: mark tokens are at least 3:1 on the surface`, () => {
      const marks = ["--tf-pick", "--tf-today", "--tf-gain-fill", "--tf-seq-3", "--tf-fam-1",
        "--tf-fam-2", "--tf-fam-3", "--tf-fam-4", "--tf-fam-5", "--tf-fam-black",
        "--tf-fam-natural", "--tf-fam-grey"]; // prettier-ignore
      for (const name of marks) {
        expect(contrast(t[name], surface), name).toBeGreaterThanOrEqual(3);
      }
    });
  }

  // Known exceptions, kept as the design's brand colours. Each is never the only carrier
  // of meaning (DESIGN 8.1: a sign, a word or a zero line always goes with it).
  it("documents the light-theme marks under 3:1", () => {
    const t = json.light;
    const low = ["--tf-loss", "--tf-out"].filter(
      (n) => contrast(t[n], t["--ba-surface"]) < 3,
    );
    expect(low).toEqual(["--tf-loss", "--tf-out"]);
  });
});
