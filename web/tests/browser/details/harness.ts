// Starts a vite dev server for the fixtures page and loads a view model into it.
import react from "@vitejs/plugin-react";
import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import type { ViteDevServer } from "vite";

const web = fileURLToPath(new URL("../../../", import.meta.url));
export const FULL = fileURLToPath(
  new URL("../../../../docs/design/race-view-model.full.json", import.meta.url),
);
export const SCREENS = fileURLToPath(
  new URL("./__screens__/", import.meta.url),
);

export type Model = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export function loadModel(): Model {
  return JSON.parse(readFileSync(FULL, "utf8")) as Model;
}

export async function startFixtures(): Promise<{
  server: ViteDevServer;
  url: string;
}> {
  const server = await createServer({
    root: web,
    configFile: false,
    plugins: [react()],
    logLevel: "error",
    server: { port: 0, host: "127.0.0.1" },
  });
  await server.listen();
  const port = (server.httpServer!.address() as AddressInfo).port;
  return { server, url: `http://127.0.0.1:${port}` };
}

export async function openFixture(
  page: Page,
  url: string,
  query: string,
  model: Model,
): Promise<void> {
  await page.addInitScript((m) => {
    (window as unknown as { __MODEL__: unknown }).__MODEL__ = m;
  }, model);
  await page.goto(`${url}/fixtures-details.html?${query}`);
  await page.waitForSelector("main.tf-root > *");
}
