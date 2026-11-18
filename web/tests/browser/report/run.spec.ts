// B-27: the run viewer in the offline file (DESIGN.md section 4).
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { fillTemplate, readJson } from "./fill";

const web = fileURLToPath(new URL("../../../", import.meta.url));
const RUN_GOLDEN = join(web, "../docs/design/run-view-model.example.json");
const SCREENS = join(web, "tests/browser/__screens__");

type Model = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const golden = () => readJson(RUN_GOLDEN) as Model;

async function open(page: Page, model: Model, name: string, query = "") {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.context().route("**", (route) => {
    const url = route.request().url();
    return url.startsWith("file:") || url.startsWith("data:")
      ? route.continue()
      : route.abort();
  });
  await page.goto(pathToFileURL(fillTemplate(model, name)).href + query);
  await expect(page.locator("[data-view='run']")).toBeVisible();
  return errors;
}

test("the hero answers the question with the real cnc-shop numbers", async ({
  page,
}) => {
  const errors = await open(page, golden(), "run.html");
  const hero = page.locator("#answer");
  await expect(hero.locator("h1")).toHaveText("Most will. Watch WO-9.");
  await expect(hero).toContainText("Will my orders finish on time?");
  await expect(hero).toContainText("of orders on time. Likely 83% to 91%.");
  await expect(hero).toContainText("Add a second shaft mill");
  await expect(hero).toContainText("+11 points");
  await expect(hero).toContainText("3 of 10");
  expect(errors).toEqual([]);
});

test("orders are sorted by late chance and every pill carries words", async ({
  page,
}) => {
  await open(page, golden(), "run.html");
  const list = page.getByRole("list", {
    name: "10 orders, sorted by the chance of being late",
  });
  const rows = list.getByRole("listitem");
  await expect(rows).toHaveCount(10);
  await expect(rows.nth(0)).toContainText("WO-9");
  await expect(rows.nth(0)).toContainText("Late · 57%");
  await expect(rows.nth(1)).toContainText("At risk · 43%");
  await expect(rows.nth(9)).toContainText("On time");
  await expect(rows.nth(0)).toContainText(
    "It waits in line at the shaft mill.",
  );
  await expect(
    rows.nth(0).getByRole("button", { name: /^WO-9: released at 20 min/ }),
  ).toBeVisible();
});

test("every bar stays inside its track", async ({ page }) => {
  await open(page, golden(), "run.html");
  const tracks = page.locator(".rv-orders .rv-track");
  for (let i = 0; i < (await tracks.count()); i++) {
    const track = await tracks.nth(i).boundingBox();
    for (const part of await tracks
      .nth(i)
      .locator(".rv-bar, .rv-whisker, .rv-due")
      .all()) {
      const box = await part.boundingBox();
      expect(box!.x + box!.width).toBeLessThanOrEqual(
        track!.x + track!.width + 1,
      );
    }
  }
});

test("pressing an order opens its steps, work and wait", async ({ page }) => {
  await open(page, golden(), "run.html");
  await page.getByRole("button", { name: /^WO-9/ }).click();
  const steps = page.getByLabel("Time at each step for WO-9");
  await expect(steps).toContainText("Shaft mill");
  await expect(steps).toContainText("Wait in line");
});

test("the choke flow marks the shaft mill and the bracket mill", async ({
  page,
}) => {
  await open(page, golden(), "run.html");
  const flow = page.locator("#choke");
  const choke = flow.locator(".rv-stepcard.rv-choke");
  await expect(choke).toHaveCount(1);
  await expect(choke).toContainText("Mill");
  await expect(choke).toContainText("99%");
  await expect(choke).toContainText("Choke point");
  await expect(flow.locator(".rv-stepcard.rv-next")).toContainText("83%");
  await expect(flow).toContainText(
    "The shaft mill is the choke point. It is busy 99% of the time.",
  );
  await flow.getByRole("button", { name: "Show as table" }).click();
  await expect(
    flow.getByRole("table", { name: "How busy each step is" }),
  ).toContainText("Choke point");
});

test("the order list can show as a table with the same numbers", async ({
  page,
}) => {
  await open(page, golden(), "run.html");
  const orders = page.locator("#orders");
  await orders.getByRole("button", { name: "Show as table" }).click();
  const table = orders.getByRole("table", { name: "Every order in numbers" });
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await expect(table.locator("tbody tr").first()).toContainText("WO-9");
});

for (const theme of ["light", "dark"]) {
  test(`no accessibility failures, ${theme}`, async ({ page }) => {
    await open(page, golden(), `run-axe-${theme}.html`, `?theme=${theme}`);
    await page.getByRole("button", { name: /^WO-9/ }).click();
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual(
      [],
    );
  });
}

test("the best fix comes first with a best tag", async ({ page }) => {
  await open(page, golden(), "run.html");
  const fix = page.locator("#fix");
  await expect(fix.locator(".rv-lever").first()).toContainText(
    "Add a second shaft mill",
  );
  await expect(fix.locator(".rv-lever").first()).toContainText("best");
  await expect(fix.locator(".rv-lever").first()).toContainText("+11.4 pts");
  await expect(fix).toContainText("Only 1 point better than");
});

test("details hold the order and centre tables", async ({ page }) => {
  await open(page, golden(), "run.html");
  const table = page.getByRole("table", { name: "Every order in numbers" });
  await expect(table.locator("tbody tr")).toHaveCount(10);
  await expect(table.locator("tbody tr").first()).toContainText("−1.1 min");
  await expect(
    page.getByRole("table", { name: "Every work centre in numbers" }),
  ).toContainText("mill_shaft");
  await expect(page.getByRole("table", { name: "Labor pools" })).toContainText(
    "floor_pool",
  );
});

test("theme toggle switches dark and light", async ({ page }) => {
  await open(page, golden(), "run.html", "?theme=light");
  const html = page.locator("html");
  await expect(html).toHaveAttribute("data-theme", "light");
  await page
    .getByRole("button", { name: "Switch light or dark theme" })
    .click();
  await expect(html).toHaveAttribute("data-theme", "dark");
});

test("one test run: no late chance, the hero says so with the command", async ({
  page,
}) => {
  const m = golden();
  m.about.reps = 1;
  m.answer.lede =
    "In this one test run, 9 of 10 orders finished on time. This is one test run. Run 30 to see how sure this is.";
  m.answer.command =
    "twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30";
  for (const o of m.orders) o.late_chance = null;
  await open(page, m, "run-one.html");
  await expect(page.locator("#answer")).toContainText(
    "This is one test run. Run 30 to see how sure this is.",
  );
  await expect(page.locator("#answer")).toContainText("--reps 30");
  await expect(page.locator("#answer")).toContainText(
    "of orders on time in this one test run.",
  );
  await expect(page.locator(".tf-status").first()).toHaveText("Late");
});

test("no due dates: the risk list becomes a plain note", async ({ page }) => {
  const m = golden();
  for (const o of m.orders) {
    o.due = null;
    o.room = null;
    o.late_chance = null;
    o.status = "ok";
  }
  await open(page, m, "run-nodue.html");
  await expect(page.locator("#orders")).toContainText(
    "No due dates in this plan, so nothing can be late.",
  );
});

test("unfinished orders show a firm banner and a Not done pill", async ({
  page,
}) => {
  const m = golden();
  m.orders[9].status = "unfinished";
  m.orders[9].finish = null;
  m.warnings = [
    "One order did not finish. Likely cause: the run ended first, or scrap at inspection.",
  ];
  await open(page, m, "run-unfinished.html");
  await expect(page.getByRole("status")).toContainText("did not finish");
  await expect(
    page.getByRole("listitem").first().locator(".tf-status"),
  ).toHaveText("Not done");
});

for (const width of [1280, 820]) {
  for (const theme of ["light", "dark"]) {
    test(`screenshot ${width} ${theme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await open(
        page,
        golden(),
        `run-${width}-${theme}.html`,
        `?theme=${theme}`,
      );
      await page.evaluate(() => document.fonts.ready);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
      mkdirSync(SCREENS, { recursive: true });
      await page.screenshot({
        path: join(SCREENS, `run-viewer-${width}-${theme}.png`),
        fullPage: true,
      });
    });
  }
}
