// B-32: the DECISIONS pages of the workspace (DESIGN.md 2.2).
// The race is written into the API's workspace folder before the tests run:
//   TWINFLOW_WORKSPACE_ROOT (default ../.twinflow-workspace, the API default).
import { expect, test } from "@playwright/test";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = process.env.TWINFLOW_WEB ?? "http://127.0.0.1:5173";
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(
  process.env.TWINFLOW_WORKSPACE_ROOT ??
    resolve(here, "../../../.twinflow-workspace"),
);
const golden = resolve(
  here,
  "../../../docs/design/race-view-model.example.json",
);
const ID = "browser-race";
const file = resolve(root, "decisions", "race", `${ID}.json`);
const screenshots =
  process.env.TWINFLOW_SCREENSHOT_DIR ?? "test-results/screenshots";

test.beforeAll(() => {
  const model = JSON.parse(readFileSync(golden, "utf8"));
  delete model._about;
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(model));
});
test.afterAll(() => rmSync(file, { force: true }));

test("open a race from the list, change the view, reload, state is restored", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(WEB);
  await expect(page.getByText("DECISIONS", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Policy races", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Which rule should we run?" }),
  ).toBeVisible();
  const row = page.getByRole("row", { name: /browser-race/ });
  await expect(row).toContainText(
    "Run Family campaign instead of Lowest cover",
  );
  await expect(row).toContainText("Very likely better");
  await page.screenshot({ path: `${screenshots}/races-list.png` });
  await row.getByRole("link").click();
  await expect(page).toHaveURL(/page=race&id=browser-race/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Family campaign",
  );
  // The section bar writes `tab`; other view state (a, b, day) rides in the same query.
  await page
    .getByRole("navigation", { name: "Sections" })
    .getByText("All rules")
    .click();
  await page.evaluate(() => {
    const url = new URL(location.href);
    url.searchParams.set("a", "family_campaign");
    url.searchParams.set("b", "lowest_cover");
    url.searchParams.set("day", "9");
    history.replaceState(null, "", url);
  });
  await page.reload();
  const params = new URL(page.url()).searchParams;
  expect(Object.fromEntries(params)).toMatchObject({
    page: "race",
    id: "browser-race",
    tab: "rules",
    a: "family_campaign",
    b: "lowest_cover",
    day: "9",
  });
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Family campaign",
  );
  await expect(
    page.getByRole("heading", {
      name: "How does every rule compare to today?",
    }),
  ).toBeInViewport();
  await page.screenshot({
    path: `${screenshots}/race-workspace.png`,
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("Export HTML downloads a file that opens offline to the same view", async ({
  page,
  browser,
}) => {
  await page.goto(`${WEB}/?page=race&id=${ID}`);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Export HTML" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(`race-${ID}.html`);
  const saved = resolve("test-results", `export-${Date.now()}.html`);
  await download.saveAs(saved);
  const offline = await browser.newContext({ offline: true });
  const view = await offline.newPage();
  await view.goto(`file://${saved}`);
  await expect(view.getByRole("heading", { level: 1 })).toContainText(
    "Family campaign",
  );
  await offline.close();
});

test("a run in the workspace is listed and opens the run viewer", async ({
  page,
}) => {
  const run = JSON.parse(
    readFileSync(
      resolve(here, "../../../docs/design/run-view-model.example.json"),
      "utf8",
    ),
  );
  const runFile = resolve(root, "decisions", "run", "browser-run.json");
  mkdirSync(dirname(runFile), { recursive: true });
  writeFileSync(runFile, JSON.stringify(run));
  try {
    await page.goto(`${WEB}/?page=runs`);
    await expect(
      page.getByRole("heading", { name: "Will my orders finish on time?" }),
    ).toBeVisible();
    await expect(page.getByText("Coming soon.")).toHaveCount(0);
    const row = page.getByRole("row", { name: /browser-run/ });
    await expect(row).toContainText("Most will. Watch WO-9.");
    await expect(row).toContainText("Most on time");
    await row.getByRole("link").click();
    await expect(page).toHaveURL(/page=run&id=browser-run/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "Most will.",
    );
  } finally {
    rmSync(runFile, { force: true });
  }
});

test("every decision view has a list in the nav", async ({ page }) => {
  await page.goto(WEB);
  for (const label of [
    "Policy races",
    "Runs",
    "Sweeps",
    "States",
    "Improve",
    "Projects",
  ])
    await expect(
      page.getByRole("button", { name: label, exact: true }),
    ).toBeVisible();
});

test("an unknown race says so in plain words", async ({ page }) => {
  await page.goto(`${WEB}/?page=race&id=does-not-exist`);
  await expect(page.getByText("Could not open this page.")).toBeVisible();
});

test("the old classic-tools URL goes to the new run and sweep pages", async ({
  page,
}) => {
  await page.goto(`${WEB}/?view=legacy`);
  await expect(page).toHaveURL(/[?&]page=runs/);
  await expect(page).not.toHaveURL(/view=legacy/);
  await expect(
    page.getByRole("heading", { name: "Will my orders finish on time?" }),
  ).toBeVisible();
  await page.goto(`${WEB}/?view=legacy&tab=sweep`);
  await expect(page).toHaveURL(/[?&]page=sweeps/);
  await expect(page.getByText("Classic simulation tools")).toHaveCount(0);
});

test("agent access lists the integrations the old tools showed", async ({
  page,
}) => {
  await page.goto(`${WEB}/?page=agents`);
  await expect(
    page.getByRole("heading", { name: "What can connect" }),
  ).toBeVisible();
});
