import { expect, test, type Page } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { startFixtures } from "../fixtures";

let server: ViteDevServer;
let base = "";
test.beforeAll(async () => {
  ({ server, url: base } = await startFixtures());
});
test.afterAll(async () => {
  await server.close();
});

const open = (page: Page, hash = "") =>
  page.goto(`${base}/fixtures/index.html?c=compare${hash}`);

test("default A is the pick and B is today", async ({ page }) => {
  await open(page);
  const card = page.getByRole("region", { name: "Compare two rules" });
  await expect(card.getByLabel("Rule A")).toHaveValue("family_campaign");
  await expect(card.getByLabel("Rule B")).toHaveValue("lowest_cover");
  await expect(card.getByRole("status")).toHaveText(
    "Family campaign makes $22,088 more per run than Lowest cover. It won 40 of 40 runs on the same orders.",
  );
});

test("Reorder point vs Lowest cover reads the result and persists in the URL", async ({
  page,
}) => {
  await open(page);
  await page.getByLabel("Rule A").selectOption("s_big_s");
  const say = page.getByRole("status");
  await expect(say).toContainText("makes $13,691 more per run");
  await expect(say).toContainText("won 36 of 40 runs");
  expect(new URL(page.url()).hash).toContain("a=s_big_s");
  await page.getByLabel("Rule B").selectOption("kanban");
  expect(new URL(page.url()).hash).toContain("b=kanban");
  await page.reload();
  await expect(page.getByLabel("Rule A")).toHaveValue("s_big_s");
  await expect(page.getByLabel("Rule B")).toHaveValue("kanban");
});

test("pill colours follow the MEASURES direction table", async ({ page }) => {
  await open(page, "#a=s_big_s&b=lowest_cover");
  const pill = (name: string) =>
    page.getByRole("row", { name: new RegExp("^" + name) }).locator(".tf-pill");
  await expect(pill("Profit per run")).toHaveClass(/tf-g/);
  await expect(pill("Changeovers per run")).toHaveClass(/tf-g/); // fewer is better
  await expect(pill("Keeping stock")).toHaveClass(/tf-l/); // A spends more
  await expect(pill("Stock on hand")).toHaveClass(/tf-n/); // neutral
  await expect(pill("Days the press rests")).toHaveClass(/tf-n/);
  const blue = await pill("Profit per run").evaluate(
    (el) => getComputedStyle(el).color,
  );
  const orange = await pill("Keeping stock").evaluate(
    (el) => getComputedStyle(el).color,
  );
  expect(blue).not.toBe(orange);
});

test("the same rule twice asks for two different rules", async ({ page }) => {
  await open(page, "#a=kanban&b=kanban");
  await expect(page.getByRole("status")).toHaveText(
    "Pick two different rules.",
  );
});

for (const width of [1280, 820]) {
  for (const scheme of ["light", "dark"] as const) {
    test(`screenshot ${width} ${scheme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: scheme });
      await open(page);
      await expect(page.getByRole("status")).toBeVisible();
      await page.screenshot({
        path: `tests/browser/__screens__/compare-${width}-${scheme}.png`,
        fullPage: true,
      });
    });
  }
}
