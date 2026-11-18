import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { TYPES, typesText } from "../../scripts/types-lib.mjs";

describe("generated types", () => {
  it("types.ts is fresh against the JSON Schemas (run `npm run gen:types`)", async () => {
    expect(readFileSync(TYPES, "utf8")).toBe(await typesText());
  });
});
