import { expect, test } from "@playwright/test";
import { open, shoot } from "./helpers";

test("the molding press is ready", async ({ page }) => {
  const errors = await open(page, "project.ready.json");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Ready. 0 problems to fix.",
  );
  await expect(page.getByText("12 products in 3 families")).toBeVisible();
  await expect(
    page.getByText("2 rules of your own, 10 built in."),
  ).toBeVisible();
  await expect(page.getByText("$1,200 and half a day")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Problems to fix", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open" })).toHaveAttribute(
    "href",
    /improve\.html$/,
  );
  expect(errors).toEqual([]);
});

test("a bad policy file shows file, line and fix before anything else", async ({
  page,
}) => {
  await open(page, "project.broken.json");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "to fix before you can race.",
  );
  const sections = page.locator("section.vk-section");
  await expect(sections.first()).toHaveAttribute("id", "problems");
  await expect(sections.first()).toContainText("policies/my_rule.py line 4");
  await expect(sections.first()).toContainText("Fix: Make decide return");
});

test("a project with no rules of its own says the built-ins will race", async ({
  page,
}) => {
  await open(page, "project.empty.json");
  await expect(page.getByText("No rules of your own yet.")).toBeVisible();
  await expect(page.getByText("No results yet.")).toBeVisible();
});

test("screenshots: ready and broken", async ({ page }) => {
  await open(page, "project.ready.json");
  await shoot(page, "project-ready");
  await open(page, "project.broken.json");
  await shoot(page, "project-broken");
});
