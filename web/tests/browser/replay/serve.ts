// Starts a Vite dev server on the web folder so Playwright can open the replay fixtures page.
// The test hands the data to the page through window.__REPLAY__ (addInitScript).
import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createServer, type ViteDevServer } from "vite";

const web = fileURLToPath(new URL("../../../", import.meta.url));
export const FULL = fileURLToPath(
  new URL("../../../../docs/design/race-view-model.full.json", import.meta.url),
);
export const SCREENS = fileURLToPath(
  new URL("../__screens__/", import.meta.url),
);

export function fullModel(): Record<string, unknown> {
  return JSON.parse(readFileSync(FULL, "utf8")) as Record<string, unknown>;
}

export async function startServer(): Promise<{
  server: ViteDevServer;
  url: string;
}> {
  const server = await createServer({
    root: web,
    configFile: false,
    logLevel: "error",
    server: { port: 0, host: "127.0.0.1" },
    plugins: [(await import("@vitejs/plugin-react")).default()],
  });
  await server.listen();
  const address = server.httpServer?.address();
  const port = typeof address === "object" && address ? address.port : 5173;
  return {
    server,
    url: `http://127.0.0.1:${port}/tests/browser/replay/fixtures.html`,
  };
}

export async function openFixture(
  page: Page,
  url: string,
  data: Record<string, unknown>,
  hash = "",
): Promise<void> {
  await page.addInitScript((d) => {
    (window as unknown as { __REPLAY__: unknown }).__REPLAY__ = d;
  }, data);
  await page.goto(url + hash);
  await page.locator("[data-ready='replay']").waitFor({ timeout: 15000 });
}
