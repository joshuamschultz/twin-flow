import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mkdirSync, writeFileSync } from "node:fs";
import type { Plugin } from "vite";

// Lists the modules in the bundle so inline-report.mjs can write the licence notices.
function moduleList(): Plugin {
  return {
    name: "twinflow-module-list",
    generateBundle() {
      // On a fresh checkout dist-report/ does not exist yet when this runs.
      mkdirSync("dist-report", { recursive: true });
      const ids = [...this.getModuleIds()].filter((id) =>
        id.includes("node_modules"),
      );
      mkdirSync("dist-report", { recursive: true });
      writeFileSync(
        "dist-report/modules.json",
        JSON.stringify(ids.sort(), null, 1),
      );
    },
  };
}

// The offline report: one HTML file, one JS chunk, one CSS file, fonts as data: URIs.
// scripts/inline-report.mjs folds the JS and CSS into the HTML and writes the template
// that Python fills (src/twinflow/report/assets/report.template.html).
export default defineConfig({
  plugins: [react(), moduleList()],
  base: "./",
  build: {
    outDir: "dist-report",
    emptyOutDir: true,
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    modulePreload: false,
    reportCompressedSize: false,
    rollupOptions: {
      input: "report.html",
      output: { inlineDynamicImports: true },
    },
  },
});
