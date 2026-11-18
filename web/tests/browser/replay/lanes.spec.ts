import { expect, test } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { fullModel, openFixture, SCREENS, startFixtures } from "../fixtures";

let server: ViteDevServer;
let url: string;

test.beforeAll(async () => {
  ({ server, url } = await startFixtures());
});
test.afterAll(async () => {
  await server.close();
});

test("set day 9: the pick lane tells the colour change and 7 of 12 out", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await openFixture(page, url, "lanes", { model: fullModel() });
  const slider = page.getByRole("slider", { name: "Day" });
  await slider.focus();
  await page.keyboard.press("Home");
  for (let i = 0; i < 8; i++) await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("9");
  await expect(page).toHaveURL(/#.*day=9/);

  const pick = page.locator("[data-lane='family_campaign']");
  await expect(
    pick.getByText("Colour change to black ($1,200, half the day lost)"),
  ).toBeVisible();
  await expect(pick.locator("[data-fact='out']")).toHaveText(/7 of 12/);
  await expect(pick.locator("[data-fact='makes']")).toHaveText(/BLK-M/);
  await expect(page.locator("[data-frame='9']")).toHaveCount(2);

  await page.screenshot({
    path: `${SCREENS}replay-lanes-1280-light.png`,
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({
    path: `${SCREENS}replay-lanes-1280-dark.png`,
    fullPage: true,
  });
});

test("keys: Page Up and Down move 10 days, End and Home jump", async ({
  page,
}) => {
  await openFixture(page, url, "lanes", {
    model: fullModel(),
    hash: "#tab=replay&day=9",
  });
  const slider = page.getByRole("slider", { name: "Day" });
  await expect(slider).toHaveValue("9");
  await slider.focus();
  await page.keyboard.press("PageUp");
  await expect(slider).toHaveValue("19");
  await page.keyboard.press("PageDown");
  await expect(slider).toHaveValue("9");
  await page.keyboard.press("End");
  await expect(slider).toHaveValue("60");
  await page.keyboard.press("Home");
  await expect(slider).toHaveValue("1");
  // Day 1 is the default, so the shared view state drops it from the URL.
  await expect(page).toHaveURL(/#tab=replay$/);
});

test("clicking a cell jumps to that day in both lanes", async ({ page }) => {
  await openFixture(page, url, "lanes", { model: fullModel() });
  await page.locator("[data-lane='lowest_cover'] [data-press]").nth(19).click();
  await expect(page.getByRole("slider", { name: "Day" })).toHaveValue("20");
  await expect(page.locator("[data-frame='20']")).toHaveCount(2);
  await expect(
    page.locator("[data-lane='family_campaign'] [data-fact='sentence']"),
  ).toHaveText(/^Day 20\./);
});

test("Play advances and stops at day 60", async ({ page }) => {
  await openFixture(page, url, "lanes", {
    model: fullModel(),
    hash: "#day=55",
  });
  const slider = page.getByRole("slider", { name: "Day" });
  await page.getByRole("button", { name: "Play" }).click();
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
  await expect(slider).toHaveValue("57", { timeout: 3000 });
  await expect(slider).toHaveValue("60", { timeout: 4000 });
  await expect(page.getByRole("button", { name: "Play" })).toBeVisible();
  await page.waitForTimeout(800);
  await expect(slider).toHaveValue("60");
});

test("reduced motion: no transitions, Play still steps", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openFixture(page, url, "lanes", {
    model: fullModel(),
    hash: "#day=58",
  });
  const btn = page.getByRole("button", { name: "Play" });
  expect(
    await btn.evaluate((el) => getComputedStyle(el).transitionDuration),
  ).toBe("0s");
  await btn.click();
  await expect(page.getByRole("slider", { name: "Day" })).toHaveValue("60", {
    timeout: 3000,
  });
});

test("lanes sit side by side at 1280 and stack at 820", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await openFixture(page, url, "lanes", { model: fullModel(), hash: "#day=9" });
  const a = page.locator("[data-lane='lowest_cover']");
  const b = page.locator("[data-lane='family_campaign']");
  let ba = (await a.boundingBox())!;
  let bb = (await b.boundingBox())!;
  expect(Math.abs(ba.y - bb.y)).toBeLessThan(2);
  expect(bb.x).toBeGreaterThan(ba.x + ba.width - 1);

  await page.setViewportSize({ width: 820, height: 1200 });
  await expect
    .poll(async () => (await b.boundingBox())!.y)
    .toBeGreaterThan((await a.boundingBox())!.y + 100);
  ba = (await a.boundingBox())!;
  bb = (await b.boundingBox())!;
  expect(Math.abs(ba.x - bb.x)).toBeLessThan(2);
  expect(bb.y).toBeGreaterThanOrEqual(ba.y + ba.height - 1);
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({
    path: `${SCREENS}replay-lanes-820-light.png`,
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({
    path: `${SCREENS}replay-lanes-820-dark.png`,
    fullPage: true,
  });
});
