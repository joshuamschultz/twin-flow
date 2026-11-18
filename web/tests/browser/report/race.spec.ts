// The race view in the offline file (TF-006 B-06 to B-14). Each test fills the committed
// template with the golden view model, or a variant of it, and opens it from file://.
import { expect, test, type Page } from "@playwright/test";
import { pathToFileURL } from "node:url";
import { fillTemplate, readJson } from "./fill";

const FULL = new URL(
  "../../../../docs/design/race-view-model.full.json",
  import.meta.url,
).pathname;
type Model = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const golden = (): Model => readJson(FULL) as Model;

async function open(page: Page, model: Model, name: string, hash = "") {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.context().route("**", (route) => {
    const url = route.request().url();
    return url.startsWith("file:") || url.startsWith("data:")
      ? route.continue()
      : route.abort();
  });
  await page.goto(pathToFileURL(fillTemplate(model, name)).href + hash);
  await expect(page.locator(".tf-hero h1"), errors.join("\n")).toBeVisible();
  return errors;
}

test("the first screen says the decision", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  const errors = await open(page, golden(), "race.html");
  const hero = page.locator(".tf-hero");
  await expect(hero.locator("h1")).toHaveText("Switch to Family campaign.");
  // Owner decision: the yearly figure from the 180-day check leads; the 60-day guess is
  // secondary text.
  const figures = hero.locator(".tf-money .tf-v");
  await expect(figures.first()).toHaveText("+$109,000");
  await expect(hero.locator(".tf-money .tf-k").first()).toHaveText(
    "more profit per year, from a 180-day check",
  );
  await expect(figures.nth(1)).toHaveText("+$22,088");
  await expect(
    hero.getByText("Quick math from the 60-day runs says +$134,400 a year."),
  ).toBeVisible();
  await expect(hero.locator(".tf-badge")).toContainText("Very likely better");
  await expect(hero.locator(".tf-caution")).toContainText(
    "One caution. The gain comes late. It is behind today's rule until day 54 of 60.",
  );
  await expect(page.locator(".tf-floor-row")).toHaveCount(5);
  await expect(page.locator(".tf-watch .tf-card")).toHaveCount(3);
  for (const id of ["why", "rules", "details"])
    await expect(page.locator(`[data-section='${id}']`)).toBeVisible();
  expect(errors).toEqual([]);
});

test("hero variants: keep, maybe, no baseline, one rule, no cautions", async ({
  page,
}) => {
  const keep = golden();
  keep.recommendation.same_as_baseline = true;
  keep.confidence.level = "same_policy";
  await open(page, keep, "keep.html");
  await expect(page.locator(".tf-hero h1")).toHaveText(
    "Keep Family campaign. Nothing tested beats it.",
  );

  const maybe = golden();
  maybe.confidence.level = "maybe_better";
  maybe.confidence.words = "Maybe better, not sure yet";
  maybe.cautions = [];
  await open(page, maybe, "maybe.html");
  await expect(page.locator(".tf-hero h1")).toHaveText(
    "Family campaign may be better. Test more before you switch.",
  );
  await expect(page.locator(".tf-watch .tf-neutral")).toContainText(
    "No cautions",
  );

  const none = golden();
  none.baseline = null;
  none.delta = null;
  await open(page, none, "nobase.html");
  await expect(page.locator(".tf-hero h1")).toHaveText(
    "Family campaign made the most money.",
  );
  await expect(page.locator(".tf-hero")).toContainText(
    "experiment.race.baseline",
  );

  const one = golden();
  one.policies = one.policies.slice(0, 1);
  await open(page, one, "one.html");
  await expect(page.locator(".tf-hero")).toContainText(
    "Only one rule ran. Add a second rule to compare.",
  );
});

test("run dots: 40 dots, focus reads one run, table lists the gaps", async ({
  page,
}) => {
  await open(page, golden(), "dots.html");
  const dots = page.locator(".tf-hero circle.tf-hit");
  await expect(dots).toHaveCount(40);
  const boxes = await dots.evaluateAll((els) =>
    els.map((e) => {
      const r = e.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + r.height / 2];
    }),
  );
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++)
      expect(
        Math.hypot(boxes[i][0] - boxes[j][0], boxes[i][1] - boxes[j][1]),
      ).toBeGreaterThanOrEqual(7);
  await dots.first().focus();
  await expect(page.getByRole("tooltip")).toHaveText(
    /^One run: \+\$[\d,]+ vs today$/,
  );
  await page.keyboard.press("ArrowRight");
  await expect(dots.nth(1)).toBeFocused();
  await page
    .locator(".tf-hero")
    .getByRole("button", { name: "Show as table" })
    .click();
  await expect(page.locator(".tf-hero table tbody tr")).toHaveCount(40);
});

test("run dots caption counts the runs today's rule won", async ({ page }) => {
  const m = golden();
  for (let i = 0; i < 6; i++) m.confidence.gaps[i] = -1500 - i * 100;
  await open(page, m, "dots-6.html");
  await expect(page.locator(".tf-dots-caption")).toHaveText(
    "Each dot is one test run. 6 of 40 dots are left of zero. In those runs today's rule did better.",
  );
});

test("why it wins: waterfall and the pull-ahead line", async ({ page }) => {
  await page.setViewportSize({ width: 820, height: 1000 });
  await open(page, golden(), "why.html");
  const rows = page.locator(".tf-wf-row");
  await expect(rows.first()).toContainText("Fewer changeovers");
  await expect(rows.last()).toContainText("Net gain per run");
  await expect(rows.last()).toContainText("+$22,088");
  const line = page.locator("[data-section='why'] svg");
  await expect(line.getByText("Day 42: $23,545 behind")).toBeVisible();
  await expect(line.getByText("Passes today on day 54")).toBeVisible();
  await expect(line.getByText("Today", { exact: true })).toBeVisible();
  // direct labels at the line ends do not overlap at 820 px
  const ends = await line.locator("text").evaluateAll((els) =>
    els
      .filter((e) =>
        [
          "Today",
          "Family campaign",
          "Reorder point",
          "Setup-aware run-out",
        ].includes(e.textContent ?? ""),
      )
      .map((e) => e.getBoundingClientRect())
      .map((r) => [r.top, r.bottom]),
  );
  ends.sort((a, b) => a[0] - b[0]);
  for (let i = 1; i < ends.length; i++)
    expect(ends[i][0]).toBeGreaterThanOrEqual(ends[i - 1][1] - 1);
});

test("charts redraw on resize without clipped text", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await open(page, golden(), "resize.html");
  const svg = page.locator("[data-section='why'] svg");
  const wide = await svg.evaluate((e) => e.getBoundingClientRect().width);
  await page.setViewportSize({ width: 820, height: 1000 });
  await expect
    .poll(() => svg.evaluate((e) => e.getBoundingClientRect().width))
    .not.toBe(wide);
  const clipped = await page.locator(".tf-root svg").evaluateAll((svgs) =>
    svgs.flatMap((s) => {
      const box = s.getBoundingClientRect();
      const card = (
        s.closest(".tf-card, .tf-hero") ?? s
      ).getBoundingClientRect();
      return [...s.querySelectorAll("text")]
        .map((t) => t.getBoundingClientRect())
        .filter(
          (r) =>
            r.left < card.left - 1 ||
            r.right > card.right + 1 ||
            r.width > box.width,
        )
        .map((r) => r.right);
    }),
  );
  expect(clipped).toEqual([]);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(820);
});

test("all rules: today is the zero row, weak rules fold, hover tells the range", async ({
  page,
}) => {
  await open(page, golden(), "rules.html");
  const board = page.locator("[data-section='rules']");
  await expect(board.locator("[data-rule='lowest_cover']")).toContainText(
    "$43,322 now",
  );
  await expect(board.locator("[data-rule='lowest_stock_first']")).toContainText(
    "same as today",
  );
  await expect(board.locator("summary")).toHaveText(
    "Show four weaker rules (two are reference floors: Random and Do nothing)",
  );
  await expect(board.locator("[data-rule='idle']")).toBeHidden();
  await board.locator("[data-rule='s_big_s']").hover();
  await expect(page.getByRole("tooltip")).toHaveText(
    "Reorder point: +$13,691 per run vs today. Likely range +$11,500 to +$15,900. Won 36 of 40 runs.",
  );
  await board.getByRole("button", { name: "Show as table" }).click();
  await expect(board.locator("table tbody tr")).toHaveCount(12);
  await expect(board.locator(".tf-takeaway")).toContainText(
    "Three rules beat today.",
  );
});

test("view state lives in the hash: theme and section survive a reload", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await open(page, golden(), "state.html");
  await page
    .getByRole("button", { name: "Switch light or dark theme" })
    .click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page
    .getByRole("navigation", { name: "Sections" })
    .getByText("All rules")
    .click();
  await expect(page).toHaveURL(/#.*theme=dark/);
  await expect(page).toHaveURL(/tab=rules/);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect
    .poll(() => page.evaluate(() => window.scrollY))
    .toBeGreaterThan(500);
});
