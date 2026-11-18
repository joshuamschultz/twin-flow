// State map and money leaks inside the offline race view (B-21, B-22 wired into RaceView).
import { expect, test, type Page } from "@playwright/test";
import { pathToFileURL } from "node:url";
import { fillTemplate } from "../report/fill";
import { axeViolations, FULL, WEB } from "../fixtures";

async function open(page: Page, width = 1280, hash = "") {
  await page.setViewportSize({ width, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.context().route("**", (route) => {
    const url = route.request().url();
    return url.startsWith("file:") || url.startsWith("data:")
      ? route.continue()
      : route.abort();
  });
  await page.goto(
    pathToFileURL(fillTemplate(FULL, "race-states.html")).href + hash,
  );
  await expect(page.locator("#states [data-state-id]").first()).toBeAttached();
  return errors;
}

test("the race view has the state map and money leaks sections", async ({
  page,
}) => {
  const errors = await open(page);
  await expect(page.locator("#states h2")).toHaveText(
    "What situations does the plant live in, and which ones cost money?",
  );
  await expect(page.locator("#leaks .tf-leak")).toHaveCount(5);
  await expect(page.locator("#states [data-state-panel] h3")).toHaveText(
    "All stock OK. Press on grey.",
  );
  await expect(page.locator("#details")).not.toContainText("Money leaks");
  expect(errors).toEqual([]);
});

test("a leak row selects its state, and the selection survives a reload", async ({
  page,
}) => {
  await open(page, 820);
  await page.locator("#states").getByRole("button", { name: "Today" }).click();
  await page.locator("#leaks .tf-leak").nth(3).click();
  const box = page.locator('#states [data-state-id="s6"]');
  await expect(box).toHaveAttribute("aria-pressed", "true");
  await expect(box).toBeInViewport();
  expect(page.url()).toContain("state=s6");
  await page.reload();
  await expect(page.locator('#states [data-state-id="s6"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

for (const theme of ["light", "dark"] as const) {
  test(`axe finds no AA problems in the race state sections (${theme})`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme });
    await open(page);
    expect(await axeViolations(page, "#states")).toEqual([]);
    expect(await axeViolations(page, "#leaks")).toEqual([]);
  });

  for (const width of [1280, 820]) {
    test(`race state sections screenshot ${width} ${theme}`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme: theme });
      await open(page, width);
      for (const id of ["states", "leaks"])
        await page.locator(`#${id}`).screenshot({
          path: `${WEB}tests/browser/__screens__/race-${id}-${width}-${theme}.png`,
          animations: "disabled",
        });
    });
  }
}
