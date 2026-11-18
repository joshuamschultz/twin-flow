// Serves the state-map fixtures page with Vite for the browser tests, and runs axe
// from a local axe-core copy (AXE_CORE_PATH) so no new npm dependency is needed.
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { createServer, type ViteDevServer } from "vite";

export const WEB = fileURLToPath(new URL("../../../", import.meta.url));
export const FULL = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL(
        "../../../../docs/design/race-view-model.full.json",
        import.meta.url,
      ),
    ),
    "utf8",
  ),
) as Record<string, unknown>;

export async function startFixtures(): Promise<{
  server: ViteDevServer;
  url: string;
}> {
  const server = await createServer({
    root: WEB,
    logLevel: "error",
    server: { port: 0, strictPort: false, host: "127.0.0.1" },
  });
  await server.listen();
  const base = server.resolvedUrls!.local[0];
  return { server, url: `${base}fixtures-statemap.html` };
}

export async function openFixture(
  page: Page,
  url: string,
  opts: { model?: unknown; theme?: "light" | "dark"; width?: number } = {},
): Promise<void> {
  await page.setViewportSize({ width: opts.width ?? 1280, height: 1000 });
  await page.emulateMedia({ colorScheme: opts.theme ?? "light" });
  await page.addInitScript((model) => {
    (window as unknown as { __TF_FIXTURE__: unknown }).__TF_FIXTURE__ = model;
  }, opts.model ?? FULL);
  await page.goto(url);
  if (opts.theme)
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, opts.theme);
  await page.locator("[data-state-map] [data-state-id]").first().waitFor();
}

export const AXE = process.env.AXE_CORE_PATH ?? "";
export const HAS_AXE = Boolean(AXE && existsSync(AXE));

type AxeViolation = {
  id: string;
  impact: string;
  nodes: { target: string[] }[];
};

export async function axeViolations(
  page: Page,
  selector: string,
): Promise<string[]> {
  await page.addScriptTag({ path: AXE });
  const found = await page.evaluate(async (sel) => {
    const axe = (
      window as unknown as {
        axe: {
          run: (
            ctx: unknown,
            opts: unknown,
          ) => Promise<{ violations: AxeViolation[] }>;
        };
      }
    ).axe;
    const res = await axe.run(sel, {
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
      },
    });
    return res.violations;
  }, selector);
  return found.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`,
  );
}
