// Compare two and What if inside the offline race file (TF-006 B-15 to B-17).
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { pathToFileURL } from "node:url";
import { fillTemplate, readJson } from "./fill";

const FULL = new URL(
  "../../../../docs/design/race-view-model.full.json",
  import.meta.url,
).pathname;

async function open(page: Page, hash = "") {
  await page.context().route("**", (route) => {
    const url = route.request().url();
    return url.startsWith("file:") || url.startsWith("data:")
      ? route.continue()
      : route.abort();
  });
  const file = fillTemplate(readJson(FULL), "race-explore.html");
  await page.goto(pathToFileURL(file).href + hash);
  await expect(page.locator(".tf-hero h1")).toBeVisible();
}

test("the section bar links Compare two and What if", async ({ page }) => {
  await open(page);
  const nav = page.locator(".tf-tabs");
  await expect(nav.getByRole("link", { name: "Compare two" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "What if" })).toBeVisible();
  await expect(page.getByText("Compare two rules side by side")).toHaveCount(0);
});

test("compare reads Reorder point vs Lowest cover and keeps a and b in the hash", async ({
  page,
}) => {
  await open(page, "#a=s_big_s");
  const section = page.locator("#compare");
  await expect(section.getByLabel("Rule A")).toHaveValue("s_big_s");
  await expect(section.getByRole("status")).toContainText(
    "makes $13,691 more per run",
  );
  await expect(section.getByRole("status")).toContainText("won 36 of 40 runs");
  await section.getByLabel("Rule B").selectOption("kanban");
  await page.reload();
  await expect(page.locator("#compare").getByLabel("Rule B")).toHaveValue(
    "kanban",
  );
});

test("what if turns amber for a new leader and resets", async ({ page }) => {
  await open(page);
  const section = page.locator("#whatif");
  await section.getByLabel("Keeping stock costs").fill("3");
  const say = section.getByRole("status");
  await expect(say).toContainText("Setup-aware run-out would win by $1,046");
  await expect(say.locator(".tf-takeaway")).toHaveClass(/tf-warn/);
  await section.getByRole("button", { name: "Reset to my costs" }).click();
  await expect(say).toContainText("Family campaign wins.");
  await expect(
    section.getByRole("note", { name: "Quick estimate" }),
  ).toBeVisible();
});

for (const scheme of ["light", "dark"] as const) {
  test(`compare and what if pass axe in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await open(page);
    await page.locator("#whatif").getByLabel("Keeping stock costs").fill("3");
    const result = await new AxeBuilder({ page })
      .include("#compare")
      .include("#whatif")
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      result.violations.map((v) => `${v.id}: ${v.nodes[0]?.target}`),
    ).toEqual([]);
  });
}

for (const width of [1280, 820]) {
  test(`screenshot of both sections at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await open(page);
    await page.locator("#compare").scrollIntoViewIfNeeded();
    await page.locator("#compare").screenshot({
      path: `tests/browser/__screens__/race-compare-${width}.png`,
    });
    await page.locator("#whatif").screenshot({
      path: `tests/browser/__screens__/race-whatif-${width}.png`,
    });
  });
}
