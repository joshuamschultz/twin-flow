// Shared by the states, improve and project browser tests: open a fixture in the built
// offline template, and take the four screenshots (1280 and 820, light and dark).
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { fillTemplate } from "../report/fill";

const here = dirname(fileURLToPath(import.meta.url));
export const SCREENS = join(here, "..", "__screens__");

export function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(here, "fixtures", name), "utf8"));
}

export async function open(page: Page, name: string): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  const file = fillTemplate(fixture(name), name.replace(".json", ".html"));
  await page.goto(pathToFileURL(file).href);
  return errors;
}

export async function shoot(page: Page, base: string): Promise<void> {
  mkdirSync(SCREENS, { recursive: true });
  for (const width of [1280, 820]) {
    for (const scheme of ["light", "dark"] as const) {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
      await page.screenshot({
        path: join(SCREENS, `${base}-${width}-${scheme}.png`),
        fullPage: true,
      });
    }
  }
}

/** WCAG 2.2 AA check in both themes; returns the rule ids that failed. */
export async function axeFailures(page: Page): Promise<string[]> {
  const out: string[] = [];
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    out.push(
      ...result.violations.map((v) => `${scheme}: ${v.id} (${v.nodes.length})`),
    );
  }
  return out;
}
