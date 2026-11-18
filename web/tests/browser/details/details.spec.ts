import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
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

test("the full table has 12 rows and the columns the design lists", async ({
  page,
}) => {
  await openFixture(page, url, "c=full-table", loadModel());
  await expect(page.getByTestId("full-table").locator("tbody tr")).toHaveCount(
    12,
  );
  await expect(
    page.getByRole("columnheader", { name: "95% range" }),
  ).toBeVisible();
  const first = page.getByTestId("full-table").locator("tbody tr").first();
  await expect(first).toContainText("Family campaign");
  await expect(first).toContainText("$64,252 to $66,568");
  await expect(first).toContainText("+$22,088");
  await expect(first).toContainText("40 of 40");
});

test("a table that is wider than the page scrolls inside its card", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 900 });
  await openFixture(page, url, "c=full-table", loadModel());
  const region = page.getByRole("region", { name: "All rules, full table" });
  const metrics = await region.evaluate((el) => ({
    scroll: el.scrollWidth,
    client: el.clientWidth,
    page: document.documentElement.scrollWidth,
  }));
  expect(metrics.scroll).toBeGreaterThan(metrics.client);
  expect(metrics.page).toBeLessThanOrEqual(600);
  await region.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => region.evaluate((el) => el.scrollLeft))
    .toBeGreaterThan(0);
});

test("at 1280 the full table fits its card, Source column included", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openFixture(page, url, "c=full-table", loadModel());
  const region = page.getByRole("region", { name: "All rules, full table" });
  const metrics = await region.evaluate((el) => ({
    scroll: el.scrollWidth,
    client: el.clientWidth,
  }));
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client);
  await expect(
    page.getByRole("columnheader", { name: "Source" }),
  ).toBeInViewport();
});

test("a broken rule lists its file, line, message and fix", async ({
  page,
}) => {
  const model = loadModel();
  model.problems = [
    {
      policy: "my_rule",
      file: "policies/my_rule.py",
      line: 12,
      message: "name 'stock' is not defined",
      fix: "Use state.stock instead of stock.",
    },
  ];
  await openFixture(page, url, "c=problems", model);
  const item = page.getByTestId("problem-list").locator("li");
  await expect(item).toHaveCount(1);
  await expect(item).toContainText("policies/my_rule.py:12");
  await expect(item).toContainText("name 'stock' is not defined");
  await expect(item).toContainText("Fix: Use state.stock instead of stock.");
  await expect(page.getByText("1 rule could not run.")).toBeVisible();
});

test("with no problems the list says so", async ({ page }) => {
  await openFixture(page, url, "c=problems", loadModel());
  await expect(
    page.getByText("No problems were found in this race."),
  ).toBeVisible();
});

test("Download race.json saves the whole model", async ({ page }) => {
  const model = loadModel();
  await openFixture(page, url, "c=download", model);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "Download race.json" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("race.json");
  const path = await download.path();
  expect(JSON.parse(readFileSync(path, "utf8"))).toEqual(model);
});

test("the Details section reads top to bottom and keeps the old report notes", async ({
  page,
}) => {
  await openFixture(page, url, "c=details", loadModel());
  const headings = await page
    .locator("#details h2, #details h3")
    .allTextContents();
  expect(headings).toEqual([
    "The full numbers",
    "Every rule, side by side",
    "What is information worth?",
    "Problems",
  ]);
  await expect(page.getByTestId("early-leader")).toContainText(
    "Biggest dollar shortfall led after day 1.",
  );
  await expect(page.getByTestId("early-leader")).toContainText(
    "passed it on day 51.",
  );
  await expect(page.getByTestId("info-worth")).toContainText(
    "Every rule saw the full view.",
  );
  await expect(page.getByTestId("run-facts")).toContainText("Runs per rule40");
});

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 820]) {
    test(`screenshot details ${theme} ${width}`, async ({ page }) => {
      const model = loadModel();
      await page.setViewportSize({ width, height: 900 });
      await openFixture(page, url, `c=details&theme=${theme}`, model);
      await page.waitForTimeout(150);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `${SCREENS}details-${theme}-${width}.png`,
        fullPage: true,
      });
    });
  }
}

test("screenshot details with a broken rule", async ({ page }) => {
  const model = loadModel();
  model.problems = [
    {
      policy: "my_rule",
      file: "policies/my_rule.py",
      line: 12,
      message: "name 'stock' is not defined",
      fix: "Use state.stock instead of stock.",
    },
    {
      policy: null,
      file: null,
      line: null,
      message: "No file was found for kanban.",
      fix: "Add policies/kanban.py.",
    },
  ];
  model.warnings = [
    {
      kind: "dollars",
      text: "Dollars were not set for two products.",
      fix: "Add a price in the project file.",
    },
  ];
  await openFixture(page, url, "c=problems", model);
  await page.screenshot({ path: `${SCREENS}details-problems-1280.png` });
});
