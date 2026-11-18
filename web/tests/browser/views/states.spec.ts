import { expect, test } from "@playwright/test";
import { open, shoot } from "./helpers";

test("the family view draws lanes and a plain headline", async ({ page }) => {
  const errors = await open(page, "states.family.json");
  await expect(page.locator("[data-view='states']")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "of its days",
  );
  await expect(page.locator("[data-state-map]")).toHaveAttribute(
    "data-mode",
    "setup",
  );
  await expect(
    page.getByRole("heading", { name: "Where does the money go?" }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("Top states changes how many boxes are drawn", async ({ page }) => {
  await open(page, "states.family.json");
  const boxes = page.locator("[data-state-id]");
  const before = await boxes.count();
  await page.getByRole("button", { name: "Less: Top states" }).click();
  await page.getByRole("button", { name: "Less: Top states" }).click();
  expect(await boxes.count()).toBe(before - 2);
});

test("a box shows where the plant goes next, with the keyboard too", async ({
  page,
}) => {
  await open(page, "states.family.json");
  const second = page.locator("[data-state-id]").nth(1);
  await second.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("[data-state-panel]")).toContainText("Stays here");
});

test("a job shop run falls back to the grid and says how to fix it", async ({
  page,
}) => {
  await open(page, "states.grid.json");
  await expect(page.locator("[data-state-map]")).toHaveAttribute(
    "data-mode",
    "grid",
  );
  await expect(page.getByText("state_groups").first()).toBeVisible();
});

test("a run with no moves shows the fix text", async ({ page }) => {
  await open(page, "states.empty.json");
  await expect(
    page.getByText("experiment.state_capture").first(),
  ).toBeVisible();
  await expect(page.locator("[data-state-id]")).toHaveCount(0);
});

test("screenshots: family view, four sizes and themes", async ({ page }) => {
  await open(page, "states.family.json");
  await shoot(page, "states");
});
