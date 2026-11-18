import { expect, test } from "@playwright/test";
import { axeFailures, open } from "./helpers";

for (const name of [
  "states.family.json",
  "states.grid.json",
  "states.empty.json",
  "improve.won.json",
  "improve.close.json",
  "project.ready.json",
  "project.broken.json",
]) {
  test(`no accessibility failures in ${name}, light and dark`, async ({
    page,
  }) => {
    await open(page, name);
    expect(await axeFailures(page)).toEqual([]);
  });
}
