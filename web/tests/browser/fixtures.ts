// The one way component tests open a part alone: a Vite dev server on web/ serves
// fixtures/index.html?c=<name> (src/fixtures/). The test hands the page a race view
// model through window.__TF_FIXTURE__; without one the page uses the golden full model.
// axeViolations runs @axe-core/playwright on part of the page.
import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer, type ViteDevServer } from "vite";

export const WEB = fileURLToPath(new URL("../../", import.meta.url));
export const SCREENS = fileURLToPath(
  new URL("./__screens__/", import.meta.url),
);
const FULL_PATH = fileURLToPath(
  new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
);

/** A fresh copy of the golden full race view model. */
export function fullModel(): Record<string, unknown> {
  return JSON.parse(readFileSync(FULL_PATH, "utf8")) as Record<string, unknown>;
}
export const FULL = fullModel();

export type FixtureName =
  "compare" | "whatif" | "statemap" | "strips" | "lanes";

/** Starts the fixtures server. `url` is the server base, with no trailing slash. */
export async function startFixtures(): Promise<{
  server: ViteDevServer;
  url: string;
}> {
  const server = await createServer({
    root: WEB,
    configFile: false,
    plugins: [react()],
    logLevel: "error",
    server: {
      port: 0,
      strictPort: false,
      host: "127.0.0.1",
      fs: { allow: [WEB + ".."] },
    },
  });
  await server.listen();
  const url = server.resolvedUrls!.local[0].replace(/\/$/, "");
  return { server, url };
}

/** What each fixture shows once it is drawn. */
const READY: Record<FixtureName, string> = {
  compare: "[data-fixture='compare']",
  whatif: "[data-fixture='whatif']",
  statemap: "[data-state-map] [data-state-id]",
  strips: "[data-ready='strips'] [data-lane]",
  lanes: "[data-ready='lanes'] [data-lane]",
};

export async function openFixture(
  page: Page,
  base: string,
  name: FixtureName,
  opts: {
    model?: unknown;
    theme?: "light" | "dark";
    width?: number;
    /** URL hash with view state, for example "#day=9". */
    hash?: string;
  } = {},
): Promise<void> {
  await page.setViewportSize({ width: opts.width ?? 1280, height: 1000 });
  if (opts.theme) await page.emulateMedia({ colorScheme: opts.theme });
  if (opts.model !== undefined)
    await page.addInitScript((model) => {
      (window as unknown as { __TF_FIXTURE__: unknown }).__TF_FIXTURE__ = model;
    }, opts.model);
  await page.goto(`${base}/fixtures/index.html?c=${name}${opts.hash ?? ""}`);
  if (opts.theme)
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, opts.theme);
  await page.locator(READY[name]).first().waitFor({ timeout: 15000 });
}

export async function axeViolations(
  page: Page,
  selector: string,
): Promise<string[]> {
  const res = await new AxeBuilder({ page })
    .include(selector)
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  return res.violations.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`,
  );
}
