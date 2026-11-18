// Fills the committed offline template the way twinflow.report.decision.render does,
// so browser tests run without Python. The Python side has its own tests.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = fileURLToPath(new URL("../../../", import.meta.url));
export const TEMPLATE = join(
  web,
  "../src/twinflow/report/assets/report.template.html",
);
export const GOLDEN = join(web, "../docs/design/race-view-model.example.json");

export function readJson(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

export function fillTemplate(model: unknown, name: string): string {
  const json = JSON.stringify(model)
    .replaceAll("</", "<\\/")
    .replaceAll(" ", "\\u2028")
    .replaceAll(" ", "\\u2029");
  const html = readFileSync(TEMPLATE, "utf8").replace(
    "/*__TWINFLOW_DATA__*/",
    () => json,
  );
  const out = join(web, "test-results/report", name);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, html);
  return out;
}
