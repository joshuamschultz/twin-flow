import { expect, test } from "@playwright/test";
const API = process.env.TWINFLOW_API_TARGET ?? "http://127.0.0.1:8000";
const screenshots =
  process.env.TWINFLOW_SCREENSHOT_DIR ?? "test-results/screenshots";

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
    page.getByRole("heading", { name: "Spring", exact: true, level: 1 }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Process map" }).click();
  await expect(page.getByText("The shape of your operation")).toBeVisible();
  await page.screenshot({
    path: `${screenshots}/process.png`,
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
  const briefName = `Browser intake ${Date.now()}`;
  await page.getByLabel("Operation name").fill(briefName);
  await page
    .getByLabel("What flows, and in what order?")
    .fill("Invoice approval then payment");
  await page.getByRole("button", { name: "Save building brief" }).click();
  await expect(page.getByRole("heading", { name: briefName })).toBeVisible();
  await page
    .getByRole("button", { name: "Scenario library", exact: true })
    .click();
  await page.screenshot({
    path: `${screenshots}/library.png`,
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
  await page
    .getByLabel("Or paste scenario content")
    .fill("part_types: []\nlocations: []\nrouting: []");
  await page.getByRole("button", { name: "Validate & import" }).click();
  await expect(page.getByRole("alert")).toContainText("legacy floor model");
  await page.getByRole("button", { name: "Close import" }).click();
  await page.screenshot({
    path: `${screenshots}/mobile.png`,
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

for (const [example, outcome] of [
  ["Office Tasks, approvals & information flow", "Case completion outcomes"],
  [
    "Supply Manufacturing Multi-tier materials, dates & evidence",
    "Delivery outlook",
  ],
]) {
  test(`domain workflow: ${example}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("http://127.0.0.1:5173");
    await page.getByRole("button", { name: example, exact: true }).click();
    await page.getByRole("tab", { name: "Process map" }).click();
    await expect(page.getByText("The shape of your operation")).toBeVisible();
    await page.getByRole("tab", { name: "Overview" }).click();
    await page.getByLabel("Replications", { exact: true }).fill("2");
    await page
      .getByRole("button", { name: "Run experiment", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: outcome, exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: `${screenshots}/${outcome.startsWith("Case") ? "office" : "supply"}-result.png`,
      fullPage: true,
    });
    expect(errors).toEqual([]);
  });
}

test("operational data reconciliation and verified scheduling", async ({
  page,
}) => {
  await page.goto("http://127.0.0.1:5173");
  await page
    .getByRole("button", { name: "Operational data", exact: true })
    .click();
  await page.getByRole("button", { name: "Load example", exact: true }).click();
  await page
    .getByRole("button", { name: "Import events", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("events inserted");
  await page
    .getByRole("button", { name: "Reconcile snapshot", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Retained evidence" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Scheduling", exact: true }).click();
  await page.getByRole("button", { name: "Load example", exact: true }).click();
  await page.getByLabel("Scheduling method").selectOption("ortools");
  await page
    .getByRole("button", { name: "Find schedule", exact: true })
    .click();
  await expect(
    page.getByText("OPTIMAL · Verified", { exact: true }),
  ).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export schedule CSV" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("schedule.csv");
  await page.screenshot({
    path: `${screenshots}/scheduling.png`,
    fullPage: true,
  });
});

test("review exact proposal and deliver a dry run", async ({
  page,
  request,
}) => {
  const loaded = await request.post(`${API}/api/workspace/examples/load`, {
    data: { example_id: "capsule-office" },
  });
  const scenario = await loaded.json();
  const evaluated = await request.post(
    `${API}/api/workspace/scenarios/${scenario.id}/evaluate`,
    {
      data: { reps: 1, seed: 42, request_key: `browser-action-${Date.now()}` },
    },
  );
  const job = await evaluated.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(`${API}/api/workspace/jobs/${job.id}`)
          ).json()
        ).status,
    )
    .toBe("completed");
  await page.goto("http://127.0.0.1:5173");
  await page
    .getByRole("button", { name: "Action review", exact: true })
    .click();
  await page.getByLabel("Evidence experiment").selectOption(job.id);
  await page
    .getByRole("button", { name: "Create proposal", exact: true })
    .click();
  await page.getByLabel("Reviewer label").first().fill("Browser reviewer");
  await page
    .getByRole("button", { name: "Approve this dry run", exact: true })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Deliver dry run", exact: true })
    .first()
    .click();
  await expect(page.getByRole("status")).toContainText(
    "No operational system was changed",
  );
  await page.screenshot({
    path: `${screenshots}/action-review.png`,
    fullPage: true,
  });
});
