import { expect, test, type Page } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { startFixtures } from "./server";

let server: ViteDevServer;
let base = "";
test.beforeAll(async () => {
  ({ server, url: base } = await startFixtures());
});
test.afterAll(async () => {
  await server.close();
});

const open = (page: Page) =>
  page.goto(`${base}/fixtures/explore.html?c=whatif`);
const HONEST =
  "This is a quick estimate. It re-prices the same 40 runs. The rules act the same as before. To let each rule adjust to new costs, change the costs in your project and rerun the race.";

test("keeping stock at 3 names Setup-aware run-out in amber, Reset restores", async ({
  page,
}) => {
  await open(page);
  const say = page.getByRole("status");
  await expect(say).toContainText("Family campaign wins.");
  const calm = await say
    .locator(".tf-takeaway")
    .evaluate((el) => getComputedStyle(el).backgroundColor);

  await page.getByLabel("Keeping stock costs").fill("3");
  await expect(say).toContainText(
    "Setup-aware run-out would win by $1,046 per run at these costs.",
  );
  await expect(say.locator(".tf-takeaway")).toHaveClass(/tf-warn/);
  const amber = await say
    .locator(".tf-takeaway")
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(amber).not.toBe(calm);
  const first = page.locator("[data-wi-row]").first();
  await expect(first).toHaveAttribute("data-wi-row", "setup_aware_runout");
  await expect(first).toContainText("up 2");

  await page.getByRole("button", { name: "Reset to my costs" }).click();
  await expect(page.getByLabel("Keeping stock costs")).toHaveValue("1");
  await expect(say).toContainText("Family campaign wins.");
  await expect(say.locator(".tf-takeaway")).not.toHaveClass(/tf-warn/);
  await expect(page.locator("[data-wi-row]").first()).toHaveAttribute(
    "data-wi-row",
    "family_campaign",
  );
});

test("each slider says where the winner flips", async ({ page }) => {
  await open(page);
  await expect(page.locator("#wi-hint-setup")).toHaveText(
    "Below 0.45x Setup-aware run-out wins.",
  );
  await expect(page.locator("#wi-hint-holding")).toHaveText(
    "Above 2.45x Setup-aware run-out wins.",
  );
  await expect(page.locator("#wi-hint-lost_sales")).toHaveText(
    "Family campaign wins anywhere on this slider.",
  );
});

test("sliders work by keyboard", async ({ page }) => {
  await open(page);
  const s = page.getByLabel("Keeping stock costs");
  await s.focus();
  await page.keyboard.press("ArrowRight");
  await expect(s).toHaveValue("1.1");
  await page.keyboard.press("End");
  await expect(s).toHaveValue("3");
  await expect(page.getByRole("status")).toContainText("Setup-aware run-out");
  await page.keyboard.press("Home");
  await expect(s).toHaveValue("0");
});

for (const width of [1280, 820, 600, 375]) {
  test(`the honest label is visible at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    const box = page.getByRole("note", { name: "Quick estimate" });
    await expect(box).toHaveText(HONEST);
    await expect(box).toBeVisible();
    await expect(box).toBeInViewport({ ratio: 0.99 });
    const clipped = await box.evaluate(
      (el) =>
        el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight,
    );
    expect(clipped).toBe(false);
  });
}

for (const width of [1280, 820]) {
  for (const scheme of ["light", "dark"] as const) {
    test(`screenshot ${width} ${scheme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
      await open(page);
      await page.getByLabel("Keeping stock costs").fill("3");
      await expect(page.getByRole("status")).toContainText("would win");
      await page.screenshot({
        path: `tests/browser/__screens__/whatif-${width}-${scheme}.png`,
        fullPage: true,
      });
    });
  }
}
