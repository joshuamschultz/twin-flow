import { expect, test } from "@playwright/test";
import { open, shoot } from "./helpers";

test("the family view draws lanes and a plain headline", async ({ page }) => {
  const errors = await open(page, "states.family.json");
  await expect(page.locator("[data-view='states']")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "of its days",
  );
  expect(await page.locator("[data-lane]").count()).toBeGreaterThanOrEqual(2);
  await expect(page.getByText("Press on")).not.toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Where does the money go?" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("Top states and Hide rare moves change what is drawn", async ({
  page,
}) => {
  await open(page, "states.family.json");
  const boxes = page.locator("[data-testid='states-map-slot'] [role='button']");
  const before = await boxes.count();
  await page.getByRole("button", { name: "Less: Top states" }).click();
  await page.getByRole("button", { name: "Less: Top states" }).click();
  expect(await boxes.count()).toBe(before - 2);
  const rows = page.locator("details table tbody tr");
  await page.locator("details summary").first().click();
  const loose = await rows.count();
  for (let i = 0; i < 6; i++)
    await page
      .getByRole("button", { name: "More: Hide rare moves under" })
      .click();
  expect(await rows.count()).toBeLessThan(loose);
});

test("a box opens where the plant goes next, with the keyboard too", async ({
  page,
}) => {
  await open(page, "states.family.json");
  const first = page
    .locator("[data-testid='states-map-slot'] [role='button']")
    .first();
  await first.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("states-next")).toContainText("Stays here");
});

test("a job shop run falls back to the grid and says how to fix it", async ({
  page,
}) => {
  await open(page, "states.grid.json");
  await expect(page.locator("[data-lane='All states']")).toBeVisible();
  await expect(page.getByText("state_groups").first()).toBeVisible();
});

test("a run with no moves shows the fix text", async ({ page }) => {
  await open(page, "states.empty.json");
  await expect(
    page.getByText("experiment.state_capture").first(),
  ).toBeVisible();
  await expect(page.locator("[data-lane]")).toHaveCount(0);
});

test("screenshots: family view, four sizes and themes", async ({ page }) => {
  await open(page, "states.family.json");
  await shoot(page, "states");
});
