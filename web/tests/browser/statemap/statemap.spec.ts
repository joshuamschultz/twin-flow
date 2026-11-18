import { expect, test } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { axeViolations, openFixture, startFixtures, WEB } from "../fixtures";

let server: ViteDevServer;
let url: string;

test.beforeAll(async () => {
  ({ server, url } = await startFixtures());
});
test.afterAll(async () => {
  await server?.close();
});

const map = "[data-fixture='statemap'] [data-state-map]";
const panel = "[data-fixture='statemap'] [data-state-panel]";
const box = (name: string) => `${map} [data-state-id][aria-label^="${name}"]`;

test("selecting a state fills the panel with time, money and next states", async ({
  page,
}) => {
  await openFixture(page, url, "statemap");
  await page.locator(box("Grey and natural high. Press on grey.")).click();
  await expect(page.locator(`${panel} h3`)).toHaveText(
    "Grey and natural high. Press on grey.",
  );
  await page.locator(box("All stock OK. Press on grey.")).click();
  const p = page.locator(panel);
  await expect(p.locator("h3")).toHaveText("All stock OK. Press on grey.");
  await expect(p.getByTestId("time")).toHaveText("8.5%");
  await expect(p).toContainText("About 5 days in a run");
  await expect(p.getByTestId("money")).toHaveText("$1,383");
  await expect(p).toContainText("A normal day makes $1,090");
  await expect(p.getByTestId("stay")).toHaveText(/Stays here\s*72%/);
  await expect(p.getByTestId("rare")).toHaveText(
    /Rare states, not on the map\s*18%/,
  );
  await expect(p).toContainText("Changeovers");
  await expect(p).toContainText("Keeping stock");
  await expect(
    page.locator(box("All stock OK. Press on grey.")),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(`${map} path.tf-sm-edge.out`)).toHaveCount(1);
  await expect(page.locator(`${map} .tf-sm-edge-label`)).toHaveText(["10%"]);
});

test("the Pick / Today switch redraws the map for today", async ({ page }) => {
  await openFixture(page, url, "statemap");
  await page
    .locator("[data-fixture='statemap']")
    .getByRole("button", { name: "Today" })
    .click();
  await expect(page.locator(`${panel} h3`)).toHaveText(
    "All stock low. Press on grey.",
  );
  await expect(page.locator(`${panel}`)).toContainText("22.9%");
  await expect(
    page.locator("[data-fixture='statemap'] [data-state-coverage]"),
  ).toHaveText(
    "The map shows the 15 most common states. They cover 91% of all days.",
  );
});

test("a next-state link moves the selection", async ({ page }) => {
  await openFixture(page, url, "statemap");
  const link = page.locator(`${panel} [data-next-state]`).first();
  const target = await link.getAttribute("data-next-state");
  const name = (await link.textContent())!;
  await link.click();
  await expect(
    page.locator(`${map} [data-state-id="${target}"]`),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(`${panel} h3`)).toHaveText(name);
});

test("arrow keys move within a lane and across lanes", async ({ page }) => {
  await openFixture(page, url, "statemap");
  const first = page.locator(box("All stock OK. Press on grey."));
  await first.focus();
  await page.keyboard.press("ArrowDown");
  const second = page.locator(`${map} [data-state-id][aria-pressed="true"]`);
  await expect(second).toBeFocused();
  await expect(second).not.toHaveAttribute(
    "aria-label",
    /^All stock OK\. Press on grey\./,
  );
  await page.keyboard.press("ArrowRight");
  await expect(
    page.locator(`${map} [data-state-id][aria-pressed="true"]`),
  ).toHaveAttribute("aria-label", /Press on black\./);
  await page.keyboard.press("Home");
  await page.keyboard.press("Enter");
  await expect(page.locator(`${panel} h3`)).toHaveText(
    "All stock OK. Press on grey.",
  );
});

test("the panel sits beside the map at 1280 and under it at 820", async ({
  page,
}) => {
  await openFixture(page, url, "statemap", { width: 1280 });
  let m = (await page.locator(map).boundingBox())!;
  let p = (await page.locator(panel).boundingBox())!;
  expect(p.x).toBeGreaterThan(m.x + m.width - 1);
  await openFixture(page, url, "statemap", { width: 820 });
  m = (await page.locator(map).boundingBox())!;
  p = (await page.locator(panel).boundingBox())!;
  expect(p.y).toBeGreaterThan(m.y + m.height - 1);
  const scroll = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(scroll).toBeLessThanOrEqual(0);
});

test("a chain with no parts falls back to the grid and still works", async ({
  page,
}) => {
  await openFixture(page, url, "statemap");
  const grid = page.locator("[data-fixture='statemap-grid']");
  await expect(grid.locator("[data-state-map]")).toHaveAttribute(
    "data-mode",
    "grid",
  );
  await grid.locator("[data-state-id]").nth(1).click();
  await expect(grid.locator("[data-state-panel] h3")).toHaveText(
    "m1:busy m2:busy.",
  );
});

for (const theme of ["light", "dark"] as const) {
  test(`axe finds no AA problems in the state map (${theme})`, async ({
    page,
  }) => {
    await openFixture(page, url, "statemap", { theme });
    expect(await axeViolations(page, "[data-fixture='statemap']")).toEqual([]);
  });

  for (const width of [1280, 820]) {
    test(`screenshot ${width} ${theme}`, async ({ page }) => {
      await openFixture(page, url, "statemap", { theme, width });
      await page.locator("[data-fixture='statemap']").screenshot({
        path: `${WEB}tests/browser/__screens__/statemap-${width}-${theme}.png`,
        animations: "disabled",
      });
    });
  }
}

test("show as table lists every state with share and money", async ({
  page,
}) => {
  await openFixture(page, url, "statemap");
  const card = page.locator("[data-fixture='statemap']");
  await card.getByRole("button", { name: "Show as table" }).first().click();
  const rows = card.locator("table.tf-table tbody tr");
  await expect(rows).toHaveCount(15);
  await expect(rows.first()).toContainText("All stock OK. Press on grey.");
  await expect(rows.first()).toContainText("8.5%");
  await expect(rows.first()).toContainText("$1,383");
});
