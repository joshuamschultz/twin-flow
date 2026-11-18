import { expect, test } from "@playwright/test";
import type { ViteDevServer } from "vite";
import { axeViolations, openFixture, startFixtures, WEB } from "./server";

let server: ViteDevServer;
let url: string;

test.beforeAll(async () => {
  ({ server, url } = await startFixtures());
});
test.afterAll(async () => {
  await server?.close();
});

const leaks = "[data-fixture='leaks']";
const map = "[data-fixture='statemap'] [data-state-map]";

test("five rows with words, cause, a try line and a stacked bar", async ({
  page,
}) => {
  await openFixture(page, url);
  const rows = page.locator(`${leaks} .tf-leak`);
  await expect(rows).toHaveCount(5);
  const first = rows.first();
  await expect(first.locator(".tf-leak-name")).toHaveText(
    "Black and natural high. Press on natural.",
  );
  await expect(first).toContainText("6.0% of days. Biggest cost: changeovers.");
  await expect(first).toContainText("Try longer runs of one family");
  await expect(first.locator(".tf-leak-val")).toHaveText("$3,949");
  await expect(first.locator(".tf-leak-stack i")).toHaveCount(2);
  await expect(rows.nth(3).locator(".tf-leak-stack i")).toHaveCount(3);
  for (const row of await rows.all())
    await expect(row.locator(".tf-leak-try")).toContainText(/Try /);
});

test("clicking a row selects that state in the map and scrolls to it", async ({
  page,
}) => {
  await openFixture(page, url, { width: 820 });
  await page
    .locator("[data-fixture='statemap']")
    .getByRole("button", { name: "Today" })
    .click();
  const row = page.locator(`${leaks} .tf-leak`).nth(3);
  await row.scrollIntoViewIfNeeded();
  await row.click();
  const box = page.locator(`${map} [data-state-id="s6"]`);
  await expect(box).toHaveAttribute("aria-pressed", "true");
  await expect(box).toBeInViewport();
  await expect(box).toBeFocused();
  await expect(
    page.locator("[data-fixture='statemap'] [data-state-panel] h3"),
  ).toHaveText("Grey high, black and natural low. Press on black.");
});

test("rows work by keyboard", async ({ page }) => {
  await openFixture(page, url);
  await page.locator(`${leaks} .tf-leak`).first().focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(`${map} [data-state-id="s5"]`)).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

for (const theme of ["light", "dark"] as const) {
  test(`axe finds no AA problems in the leak list (${theme})`, async ({
    page,
  }) => {
    await openFixture(page, url, { theme });
    expect(await axeViolations(page, leaks)).toEqual([]);
  });

  for (const width of [1280, 820]) {
    test(`leaks screenshot ${width} ${theme}`, async ({ page }) => {
      await openFixture(page, url, { theme, width });
      await page.locator(leaks).screenshot({
        path: `${WEB}tests/browser/__screens__/leaks-${width}-${theme}.png`,
        animations: "disabled",
      });
    });
  }
}
