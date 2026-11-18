// B-25: the race view review gate. The whole offline file, built from the full golden
// view model: section order, every explore section wired in, URL state for a, b, day and
// state, keyboard use, axe in both themes, screenshots for the mockup review, offline, size.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { statSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { FULL, SCREENS } from "../fixtures";
import { fillTemplate } from "./fill";

const SECTION_BAR = [
  "Decision",
  "Why it wins",
  "All rules",
  "Compare two",
  "What if",
  "Floor replay",
  "State map",
  "Money leaks",
  "Tuning",
  "Details",
];

let file = "";
test.beforeAll(() => {
  file = fillTemplate(FULL, "race-full.html");
});

async function open(page: Page, hash = "", width = 1280) {
  await page.setViewportSize({ width, height: 1000 });
  const errors: string[] = [];
  const blocked: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.context().route("**", (route) => {
    const url = route.request().url();
    if (url.startsWith("file:") || url.startsWith("data:"))
      return route.continue();
    blocked.push(url);
    return route.abort();
  });
  await page.goto(pathToFileURL(file).href + hash);
  await expect(page.locator(".tf-hero h1")).toBeVisible();
  await expect(page.locator("#states [data-state-id]").first()).toBeAttached();
  return { errors, blocked };
}

test("the section bar follows the design order and every section is there", async ({
  page,
}) => {
  const { errors, blocked } = await open(page);
  const nav = page.getByRole("navigation", { name: "Sections" });
  await expect(nav.getByRole("link")).toHaveText(SECTION_BAR);
  for (const id of [
    "why",
    "rules",
    "compare",
    "whatif",
    "replay",
    "states",
    "leaks",
    "tuning",
    "details",
  ])
    await expect(page.locator(`#${id}`)).toBeAttached();
  await expect(
    page.getByText("More of this report is on the way."),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
  expect(blocked).toEqual([]);
});

test("hero buttons jump to why, the floor replay and what if", async ({
  page,
}) => {
  await open(page);
  const hero = page.locator(".tf-hero");
  await hero.getByRole("link", { name: "Watch the floor" }).click();
  await expect(page).toHaveURL(/tab=replay/);
  await expect(page.locator("#replay h2")).toBeInViewport();
  await hero.getByRole("link", { name: "Test my cost numbers" }).click();
  await expect(page).toHaveURL(/tab=whatif/);
  await hero.getByRole("link", { name: "See why it wins" }).click();
  await expect(page).toHaveURL(/tab=why/);
});

test("floor replay in the race view: keys set the day and it goes in the URL", async ({
  page,
}) => {
  await open(page);
  const replay = page.locator("#replay");
  const slider = replay.getByRole("slider", { name: "Replay day" });
  await slider.focus();
  await page.keyboard.press("Home");
  for (let i = 0; i < 8; i++) await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("9");
  await expect(page).toHaveURL(/day=9/);
  const pick = replay.locator("[data-lane='family_campaign']");
  await expect(
    pick.getByText("Colour change to black ($1,200, half the day lost)"),
  ).toBeVisible();
  await expect(pick.locator("[data-fact='out']")).toHaveText(/7 of 12/);
});

test("a link with a, b, day and state reopens the same picture, and a reload keeps it", async ({
  page,
}) => {
  const check = async () => {
    await expect(page.locator("#compare").getByLabel("Rule A")).toHaveValue(
      "s_big_s",
    );
    await expect(page.locator("#compare").getByLabel("Rule B")).toHaveValue(
      "kanban",
    );
    await expect(
      page.locator("#replay").getByRole("slider", { name: "Replay day" }),
    ).toHaveValue("20");
    await expect(page.locator('#states [data-state-id="s6"]')).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  };
  await open(page, "#a=s_big_s&b=kanban&day=20&state=s6");
  await check();
  await page.reload();
  await check();
});

test("keyboard only: theme toggle, section links and the state map", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "light" });
  await open(page);
  const toggle = page.getByRole("button", {
    name: "Switch light or dark theme",
  });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const link = page
    .getByRole("navigation", { name: "Sections" })
    .getByRole("link", { name: "State map" });
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/tab=states/);
  const first = page.locator("#states [data-state-id]").first();
  await first.focus();
  await page.keyboard.press("Enter");
  await expect(first).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/state=/);
});

for (const theme of ["light", "dark"] as const) {
  test(`axe finds no AA problems in the whole race view (${theme})`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme });
    await open(page, `#theme=${theme}`);
    const res = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const found = res.violations.map(
      (v) =>
        `${v.id} (${v.impact}): ${v.nodes
          .slice(0, 5)
          .map((n) => n.target.join(" "))
          .join(", ")}`,
    );
    expect(found).toEqual([]);
  });

  for (const width of [1280, 820]) {
    test(`race view screenshot ${width} ${theme}`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await open(page, `#theme=${theme}`, width);
      await page.screenshot({
        path: `${SCREENS}race-full-${width}-${theme}.png`,
        fullPage: true,
        animations: "disabled",
      });
      await page.screenshot({
        path: `${SCREENS}race-first-${width}-${theme}.png`,
        animations: "disabled",
      });
    });
  }
}

test("the full race file stays under 1.2 MB", () => {
  expect(statSync(file).size).toBeLessThan(1.2 * 1024 * 1024);
});
