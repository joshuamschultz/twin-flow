import { expect, test } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { longRun } from "../../fixtures/replayLong";
import { fullModel, openFixture, SCREENS, startFixtures } from "../fixtures";

let server: ViteDevServer;
let url: string;

test.beforeAll(async () => {
  ({ server, url } = await startFixtures());
});
test.afterAll(async () => {
  await server.close();
});

const ORANGE = "rgb(246, 141, 46)";

test("golden run: colour changes and out-of-stock cells per lane", async ({
  page,
}) => {
  await openFixture(page, url, "strips", { model: fullModel() });
  const today = page.locator("[data-lane='lowest_cover']");
  const pick = page.locator("[data-lane='family_campaign']");

  await expect(today.locator("[data-change='colour']")).toHaveCount(41);
  await expect(pick.locator("[data-change='colour']")).toHaveCount(9);
  await expect(today.locator("[data-press]")).toHaveCount(60);

  const outToday = today.locator("[data-level='out']");
  const outPick = pick.locator("[data-level='out']");
  await expect(outToday).toHaveCount(124);
  await expect(outPick).toHaveCount(53);
  expect(
    await outToday.first().evaluate((el) => getComputedStyle(el).fill),
  ).toBe(ORANGE);
  expect(
    await today
      .locator("[data-change='colour']")
      .first()
      .evaluate((el) => getComputedStyle(el).fill),
  ).toBe(ORANGE);

  await expect(today.getByRole("img")).toHaveAttribute(
    "aria-label",
    /changes colour 41 times.*124 product-days out of stock/,
  );

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({
    path: `${SCREENS}replay-strips-1280-light.png`,
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({
    path: `${SCREENS}replay-strips-1280-dark.png`,
    fullPage: true,
  });
});

test("hovering a cell shows its sentence", async ({ page }) => {
  await openFixture(page, url, "strips", { model: fullModel() });
  const pick = page.locator("[data-lane='family_campaign']");
  await pick.locator("[data-press]").nth(8).hover();
  await expect(page.getByRole("tooltip")).toHaveText(
    "Day 9: makes BLK-M. Colour change to black ($1,200, half the day lost).",
  );
  await pick.locator("[data-row='BLK-S'] [data-level]").nth(8).hover();
  await expect(page.getByRole("tooltip")).toHaveText(
    "BLK-S, day 9: out of stock.",
  );
});

test("a 200-day run draws week cells and the table keeps day rows", async ({
  page,
}) => {
  const model = fullModel() as {
    products: { id: string; family: string; demand_rate: number }[];
  };
  const run = longRun(200, model.products as never);
  await openFixture(page, url, "strips", {
    model: {
      ...model,
      example_runs: { lowest_cover: run, family_campaign: run },
    },
  });
  const lane = page.locator("[data-lane='lowest_cover']");
  await expect(lane.locator("[data-press]")).toHaveCount(29);
  await expect(lane.locator("[data-press]").first()).toHaveAttribute(
    "data-days",
    "1-7",
  );

  await lane.getByRole("button", { name: "Show as table" }).click();
  const rows = lane.locator("table tbody tr");
  await expect(rows).toHaveCount(200);
  await expect(rows.nth(8).locator("td").first()).toHaveText("9");
  await lane.getByRole("button", { name: "Show as chart" }).click();
  await expect(lane.getByRole("img")).toBeVisible();
});

test("more than 30 products group by family and a family opens on click", async ({
  page,
}) => {
  const model = fullModel() as Record<string, unknown> & {
    products: { id: string; family: string; demand_rate: number }[];
  };
  const products = Array.from({ length: 36 }, (_, i) => ({
    id: `P-${i + 1}`,
    family: ["grey", "black", "natural"][i % 3],
    demand_rate: 10,
  }));
  const run = longRun(60, products as never);
  await openFixture(page, url, "strips", {
    model: {
      ...model,
      products,
      example_runs: { lowest_cover: run, family_campaign: run },
    },
  });
  const lane = page.locator("[data-lane='lowest_cover']");
  await expect(lane.locator("[data-row]")).toHaveCount(3);
  await lane.getByRole("button", { name: "Show black products" }).click();
  await expect(lane.locator("[data-row]")).toHaveCount(15);
  await expect(
    lane.getByRole("button", { name: "Hide black products" }),
  ).toHaveAttribute("aria-expanded", "true");
});
