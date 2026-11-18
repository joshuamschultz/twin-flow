import { defineConfig } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// The workspace suites (workspace.spec.ts, decisions.spec.ts) need the API and the
// dev server. Unless TWINFLOW_WEB points at a server you started yourself, the suite
// starts its own pair on free ports, with its own workspace folder. Port 8000 is
// often taken by other programs, so it is not used here.
const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..");
const external = Boolean(process.env.TWINFLOW_WEB);
const API_PORT = Number(process.env.TWINFLOW_TEST_API_PORT ?? 8765);
const WEB_PORT = Number(process.env.TWINFLOW_TEST_WEB_PORT ?? 5199);
if (!external) {
  process.env.TWINFLOW_WEB = `http://127.0.0.1:${WEB_PORT}`;
  process.env.TWINFLOW_API_TARGET = `http://127.0.0.1:${API_PORT}`;
  process.env.TWINFLOW_WORKSPACE_ROOT ??= resolve(
    here,
    "test-results",
    ".workspace",
  );
}
const env = { ...process.env, VIRTUAL_ENV: "" } as Record<string, string>;

export default defineConfig({
  testDir: "./tests/browser",
  timeout: 60000,
  workers: 1,
  use: {
    browserName: "chromium",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    },
  },
  webServer: external
    ? undefined
    : [
        {
          command: `uv run twinflow serve --port ${API_PORT} --workspace-root "${process.env.TWINFLOW_WORKSPACE_ROOT}"`,
          cwd: repo,
          env,
          url: `http://127.0.0.1:${API_PORT}/openapi.json`,
          reuseExistingServer: false,
          timeout: 120000,
        },
        {
          command: `npx vite --port ${WEB_PORT} --strictPort --host 127.0.0.1`,
          cwd: here,
          env,
          url: `http://127.0.0.1:${WEB_PORT}`,
          reuseExistingServer: false,
          timeout: 60000,
        },
      ],
});
