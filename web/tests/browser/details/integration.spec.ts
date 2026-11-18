// Tuning and Details inside the offline race view, plus accessibility in both themes.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { pathToFileURL } from "node:url";
import { fillTemplate } from "../report/fill";
import { loadModel } from "./harness";

for (const theme of ["light", "dark"]) {
  test(`tuning and details work in the offline file (${theme}) and pass axe`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.context().route("**", (route) => {
      const url = route.request().url();
      return url.startsWith("file:") || url.startsWith("data:")
        ? route.continue()
        : route.abort();
    });
    await page.setViewportSize({ width: 1280, height: 1000 });
    await page.goto(
      pathToFileURL(fillTemplate(loadModel(), `details-${theme}.html`)).href +
        `#theme=${theme}`,
    );
    await expect(page.locator("[data-section='tuning']")).toBeVisible();
    await expect(page.locator("[data-section='details']")).toBeVisible();
    await expect(
      page.getByText("More of this report is on the way"),
    ).toHaveCount(0);
    await expect(
      page.getByTestId("full-table").locator("tbody tr"),
    ).toHaveCount(12);
    await page.locator("#tuning").scrollIntoViewIfNeeded();
    await page.getByRole("button", { name: "Lowest cover" }).click();
    await expect(page.locator("#tuning .tf-takeaway")).toContainText(
      "Stop days 10 gave the most profit",
    );
    const result = await new AxeBuilder({ page })
      .include("#tuning")
      .include("#details")
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    expect(
      result.violations.map(
        (v) =>
          `${v.id}: ${v.nodes.length} ${JSON.stringify(v.nodes.map((n) => [n.html.slice(0, 90), n.any[0]?.message]))}`,
      ),
    ).toEqual([]);
    expect(errors).toEqual([]);
  });
}
