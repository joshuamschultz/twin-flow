// B-35: the sweep view in the offline file.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { fillTemplate, readJson } from "./fill";

const web = fileURLToPath(new URL("../../../", import.meta.url));
const GOLDEN = join(web, "../docs/design/sweep-view-model.example.json");
const SCREENS = join(web, "tests/browser/__screens__");

test("the cnc-shop sweep puts the second shaft mill first", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(
    pathToFileURL(fillTemplate(readJson(GOLDEN), "sweep.html")).href,
  );
  await expect(page.locator("[data-view='sweep']")).toBeVisible();
  await expect(page.locator("#answer h1")).toHaveText(
    "Add a second shaft mill. 87% to 99% on time.",
  );
  const first = page.locator("#fix .rv-lever").first();
  await expect(first).toContainText("Add a second shaft mill");
  await expect(first).toContainText("87% today → 99% on time");
  await expect(
    page.getByRole("table", { name: "Every point tested" }).locator("tbody tr"),
  ).toHaveCount(3);
  expect(errors).toEqual([]);
});

for (const theme of ["light", "dark"]) {
  test(`sweep screenshot 1280 ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(
      pathToFileURL(fillTemplate(readJson(GOLDEN), `sweep-${theme}.html`))
        .href + `?theme=${theme}`,
    );
    await expect(page.locator("[data-view='sweep']")).toBeVisible();
    mkdirSync(SCREENS, { recursive: true });
    await page.screenshot({
      path: join(SCREENS, `sweep-1280-${theme}.png`),
      fullPage: true,
    });
  });
}

for (const theme of ["light", "dark"]) {
  test(`sweep has no accessibility failures, ${theme}`, async ({ page }) => {
    await page.goto(
      pathToFileURL(fillTemplate(readJson(GOLDEN), `sweep-axe-${theme}.html`))
        .href + `?theme=${theme}`,
    );
    await expect(page.locator("[data-view='sweep']")).toBeVisible();
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.length}`)).toEqual(
      [],
    );
  });
}
