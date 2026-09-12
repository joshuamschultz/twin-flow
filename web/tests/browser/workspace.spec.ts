import { expect, test } from "@playwright/test";

test("operator imports, maps, runs and exports a scenario", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:5173");
  await expect(
    page.getByRole("heading", { name: /Understand today/ }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Spring Production flow & resource capacity" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Spring", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Process map" }).click();
  await expect(page.getByText("The shape of your operation")).toBeVisible();
  await page.screenshot({
    path: "/private/tmp/twinflow-workspace-process.png",
    fullPage: true,
  });
  await page.getByRole("tab", { name: "Overview" }).click();
  await page.getByLabel("Replications", { exact: true }).fill("2");
  await page
    .getByRole("button", { name: "Run experiment", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Order completion outcomes" }),
  ).toBeVisible({ timeout: 45000 });
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Evidence", exact: true }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^experiment-/);
  await page.getByRole("button", { name: "Build a twin", exact: true }).click();
  await page.getByLabel("Operation name").fill("Browser intake");
  await page
    .getByLabel("What flows, and in what order?")
    .fill("Invoice approval then payment");
  await page.getByRole("button", { name: "Save building brief" }).click();
  await expect(
    page.getByRole("heading", { name: "Browser intake" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Scenario library", exact: true })
    .click();
  await page.screenshot({
    path: "/private/tmp/twinflow-workspace-library.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("invalid import is actionable and mobile layout remains accessible", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:5173");
  await page
    .getByRole("button", { name: "Import scenario", exact: true })
    .click();
  await page.getByLabel("Scenario name", { exact: true }).fill("Invalid");
  await page.getByLabel("Or paste scenario content").fill("{}");
  await page.getByRole("button", { name: "Validate & import" }).click();
  await expect(page.getByRole("alert")).toContainText("missing required");
  await page.getByRole("button", { name: "Close import" }).click();
  await page.screenshot({
    path: "/private/tmp/twinflow-workspace-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
