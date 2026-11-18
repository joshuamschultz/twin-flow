import { expect, test } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { loadModel, openFixture, SCREENS, startFixtures } from "./harness";

let server: ViteDevServer;
let url: string;

test.beforeAll(async () => {
  ({ server, url } = await startFixtures());
});
test.afterAll(async () => {
  await server.close();
});

test("the curve marks the chosen setting and has an accessible name", async ({
  page,
}) => {
  await openFixture(
    page,
    url,
    "c=tuning-curve&policy=family_campaign",
    loadModel(),
  );
  const chart = page.getByRole("img", {
    name: /Profit per run for each target days/,
  });
  await expect(chart).toBeVisible();
  await expect(chart).toHaveAttribute(
    "aria-label",
    /Chosen: target days 16, \$67,186/,
  );
  await expect(page.locator("circle[data-chosen='true']")).toHaveCount(1);
  await expect(page.getByText("Chosen: $67,186")).toBeVisible();
});

test("the table view shows the same numbers as the chart", async ({ page }) => {
  const model = loadModel();
  await openFixture(page, url, "c=tuning-curve&policy=family_campaign", model);
  await page.getByRole("button", { name: "Show as table" }).click();
  const rows = page
    .getByRole("region", { name: "Settings tried, as a table" })
    .locator("tbody tr");
  const trials = model.tuning.find(
    (t: { policy: string }) => t.policy === "family_campaign",
  ).trials;
  await expect(rows).toHaveCount(trials.length);
  for (const [i, t] of trials.entries()) {
    const cells = rows.nth(i).locator("td");
    await expect(cells.nth(1)).toHaveText(
      `$${t.dollars.toLocaleString("en-US")}`,
    );
    if (t.chosen) await expect(cells.nth(2)).toHaveText("Chosen");
  }
});

test("a hover opens a tooltip with the setting and the profit", async ({
  page,
}) => {
  await openFixture(
    page,
    url,
    "c=tuning-curve&policy=family_campaign",
    loadModel(),
  );
  await page.locator("circle.tf-tune-dot").nth(1).hover();
  await expect(page.locator(".tf-tune-tip")).toHaveText(
    "target days 8: $65,505 per run",
  );
});

test("a two-setting rule draws small multiples", async ({ page }) => {
  await openFixture(
    page,
    url,
    "c=tuning-curve&policy=setup_aware_runout",
    loadModel(),
  );
  expect(await page.getByTestId("tuning-panel").count()).toBeGreaterThan(1);
  await expect(page.locator(".tf-tune-title").first()).toContainText(
    "within penalty days",
  );
});

test("the table lists every rule with a touchy or steady pill from the model", async ({
  page,
}) => {
  const model = loadModel();
  await openFixture(page, url, "c=tuning-table", model);
  const rows = page.getByTestId("tuning-table").locator("tbody tr");
  await expect(rows).toHaveCount(model.tuning.length);
  const touchy = model.tuning.filter(
    (t: { touchy: boolean }) => t.touchy,
  ).length;
  await expect(page.locator(".tf-pill.is-touchy")).toHaveCount(touchy);
  await expect(page.locator(".tf-pill.is-touchy").first()).toHaveText("touchy");
  await expect(page.locator(".tf-tag-pick")).toHaveCount(1);
});

test("the section changes its chart when a rule is picked", async ({
  page,
}) => {
  await openFixture(page, url, "c=tuning", loadModel());
  await expect(
    page.getByRole("heading", { name: "Did each rule get a fair try?" }),
  ).toBeVisible();
  await expect(page.getByTestId("tuning-takeaway")).toContainText(
    "Target days 16 gave the most profit",
  );
  await page.getByRole("button", { name: "Lowest cover" }).click();
  await expect(
    page.getByRole("heading", { level: 3, name: /Lowest cover/ }),
  ).toBeVisible();
  await expect(page.getByTestId("tuning-takeaway")).toContainText(
    "Stop days 10 gave the most profit",
  );
});

test("the section shows a dashed box when nothing was tuned", async ({
  page,
}) => {
  const model = loadModel();
  model.tuning = [];
  await openFixture(page, url, "c=tuning", model);
  await expect(page.getByTestId("tuning-none")).toContainText(
    "nothing was tuned",
  );
});

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 820]) {
    test(`screenshot tuning ${theme} ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openFixture(page, url, `c=tuning&theme=${theme}`, loadModel());
      await page.waitForTimeout(150);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `${SCREENS}tuning-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("resizing from 1280 to 820 redraws the chart without cut-off text", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openFixture(
    page,
    url,
    "c=tuning-curve&policy=family_campaign",
    loadModel(),
  );
  const box = page.locator(".tf-tune-svg").first();
  const wide = (await box.boundingBox())!.width;
  await page.setViewportSize({ width: 820, height: 900 });
  await page.waitForTimeout(200);
  const narrow = (await box.boundingBox())!.width;
  expect(narrow).toBeLessThan(wide);
  const view = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(view).toBeLessThanOrEqual(820);
});

test("screenshot two-setting small multiples", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 });
  await openFixture(
    page,
    url,
    "c=tuning-curve&policy=setup_aware_runout",
    loadModel(),
  );
  await page.waitForTimeout(150);
  await page.screenshot({ path: `${SCREENS}tuning-multiples-1280.png` });
});
