import { expect, test } from "@playwright/test";
import { open, shoot } from "./helpers";

test("a win says yes and every check passes", async ({ page }) => {
  const errors = await open(page, "improve.won.json");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "is now your best rule",
  );
  await expect(page.getByText("Pass", { exact: true })).toHaveCount(4);
  await expect(page.getByText("Fail", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("group", { name: /Each dot is one test run/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open the full race" }),
  ).toHaveAttribute("href", "race.html");
  expect(errors).toEqual([]);
});

test("ahead but not proven shows the range and a failed proof", async ({
  page,
}) => {
  await open(page, "improve.close.json");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Not yet.",
  );
  await expect(page.locator("[data-view='improve']")).toContainText(
    "-$1,942 to $12,446",
  );
  await expect(page.getByText("Fail", { exact: true })).toHaveCount(1);
  await expect(page.getByText("Nothing was copied")).toBeVisible();
});

test("a loss says no and keeps the old best", async ({ page }) => {
  await open(page, "improve.lost.json");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "is still your best rule",
  );
  await expect(page.getByText("Fail", { exact: true })).not.toHaveCount(0);
});

test("screenshots: win and close call", async ({ page }) => {
  await open(page, "improve.won.json");
  await shoot(page, "improve-won");
  await open(page, "improve.close.json");
  await shoot(page, "improve-close");
});
