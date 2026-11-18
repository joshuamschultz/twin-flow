// The details tests open parts through the one fixtures page (tests/browser/fixtures.ts).
import type { Page } from "@playwright/test";
import { fullModel, startFixtures } from "../fixtures";

export { startFixtures };
export const SCREENS = new URL("./__screens__/", import.meta.url).pathname;

export type Model = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export function loadModel(): Model {
  return fullModel() as Model;
}

/** `query` is the fixtures query without the leading "?", for example "c=tuning". */
export async function openFixture(
  page: Page,
  url: string,
  query: string,
  model: Model,
): Promise<void> {
  await page.addInitScript((m) => {
    (window as unknown as { __TF_FIXTURE__: unknown }).__TF_FIXTURE__ = m;
  }, model);
  await page.goto(`${url}/fixtures/index.html?${query}`);
  await page.waitForSelector("main.tf-root > *");
}
