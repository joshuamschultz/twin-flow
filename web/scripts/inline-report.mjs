// Folds the offline report build (dist-report/) into one HTML template for Python.
//
//   npm run build:report   build, check, write src/twinflow/report/assets/report.template.html
//   npm run check:report   build, check, and fail if the committed template differs
//
// Checks: no network URL in src, href or url(); every http(s) string is an XML namespace
// name (React uses them for createElementNS, they are never fetched); the four Python
// placeholders appear once; the size budget holds. No dependencies (DESIGN.md 11.1).
import { createHash } from "node:crypto";
import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const web = fileURLToPath(new URL("..", import.meta.url));
const repo = join(web, "..");
const dist = join(web, "dist-report");
const assets = join(repo, "src/twinflow/report/assets");
const TEMPLATE = join(assets, "report.template.html");
const LICENSES = join(assets, "THIRD-PARTY-LICENSES.txt");
export const BUDGET = 500 * 1024; // test budget; DESIGN.md 10 allows 900 KB with fonts
export const PLACEHOLDERS = [
  "/*__TWINFLOW_DATA__*/",
  "__TWINFLOW_TITLE__",
  "__TWINFLOW_DESCRIPTION__",
  "<!--__TWINFLOW_NOSCRIPT__-->",
];
export const NAMESPACES = [
  "http://www.w3.org/1999/xhtml",
  "http://www.w3.org/1999/xlink",
  "http://www.w3.org/2000/svg",
  "http://www.w3.org/1998/Math/MathML",
  "http://www.w3.org/XML/1998/namespace",
  "http://www.w3.org/2000/xmlns/",
];

/** sha256 over the template inputs listed in report.sources.json (mirrored in Python). */
export function sourceHash() {
  const spec = JSON.parse(
    readFileSync(join(web, "report.sources.json"), "utf8"),
  );
  const files = [...spec.files];
  const walk = (dir) => {
    for (const name of readdirSync(join(web, dir))) {
      const rel = `${dir}/${name}`;
      if (statSync(join(web, rel)).isDirectory()) walk(rel);
      else files.push(rel);
    }
  };
  spec.dirs.forEach(walk);
  const outer = createHash("sha256");
  for (const rel of [...new Set(files)].sort()) {
    const inner = createHash("sha256")
      .update(readFileSync(join(web, rel)))
      .digest("hex");
    outer.update(`${rel}\n${inner}\n`);
  }
  return outer.digest("hex");
}

export function networkProblems(html) {
  const problems = [];
  const attr = /\b(?:src|href|action|poster)\s*=\s*["']?\s*(?:https?:)?\/\//gi;
  const css = /url\(\s*["']?\s*(?:https?:)?\/\//gi;
  if (attr.test(html)) problems.push("a src or href points to the network");
  if (css.test(html)) problems.push("a CSS url() points to the network");
  for (const [hit] of html.matchAll(/https?:\/\/[^\s"'`<>)\\]*/g)) {
    if (
      !NAMESPACES.some(
        (ns) => hit.startsWith(ns) && hit.length <= ns.length + 1,
      )
    ) {
      problems.push(`network URL in the file: ${hit.slice(0, 80)}`);
    }
  }
  return problems;
}

function fold() {
  let html = readFileSync(join(dist, "report.html"), "utf8");
  const script = html.match(
    /<script type="module"[^>]*src="\.\/(assets\/[^"]+\.js)"[^>]*><\/script>\s*/,
  );
  const style = html.match(
    /<link rel="stylesheet"[^>]*href="\.\/(assets\/[^"]+\.css)"[^>]*>\s*/,
  );
  if (!script || !style)
    throw new Error("dist-report/report.html has no script or stylesheet tag");
  let js = readFileSync(join(dist, script[1]), "utf8");
  let cssText = readFileSync(join(dist, style[1]), "utf8");
  // React's error text links to its decoder page. Keep the words, drop the scheme.
  js = js.replaceAll(
    "https://reactjs.org/docs/error-decoder.html",
    "reactjs.org/docs/error-decoder.html",
  );
  js = js.replace(/<\/(script)/gi, "<\\/$1");
  cssText = cssText.replace(/<\/(style)/gi, "<\\/$1");
  html = html.replace(script[0], "").replace(style[0], "");
  html = html.replace(
    "</head>",
    () =>
      `    <meta name="twinflow-template-source" content="sha256:${sourceHash()}" />\n` +
      `    <style>${cssText}</style>\n  </head>`,
  );
  html = html.replace(
    "</body>",
    () => `  <script type="module">${js}</script>\n  </body>`,
  );
  return html;
}

function licenses() {
  const ids = JSON.parse(readFileSync(join(dist, "modules.json"), "utf8"));
  const packages = new Set();
  for (const id of ids) {
    const m = id
      .split(sep)
      .join("/")
      .match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
    if (m) packages.add(m[1]);
  }
  const parts = [
    "Third-party software in the twinflow offline report (report.template.html).",
    "Each section is the licence file shipped with the package or font.",
    "",
  ];
  for (const name of [...packages].sort()) {
    const dir = join(web, "node_modules", name);
    const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    const file = readdirSync(dir).find((f) => /^licen[sc]e/i.test(f));
    const text = file
      ? readFileSync(join(dir, file), "utf8").trim()
      : `License: ${pkg.license}`;
    parts.push(`=== ${name} ${pkg.version} (${pkg.license}) ===`, text, "");
  }
  const fonts = join(web, "src/decision/fonts");
  for (const file of readdirSync(fonts)
    .filter((f) => f.endsWith("-OFL.txt"))
    .sort()) {
    const name = file.replace("-OFL.txt", "");
    parts.push(
      `=== font: ${name} (OFL-1.1) ===`,
      readFileSync(join(fonts, file), "utf8").trim(),
      "",
    );
  }
  return parts.join("\n");
}

function main() {
  const check = process.argv.includes("--check");
  const html = fold();
  const problems = networkProblems(html);
  for (const p of PLACEHOLDERS) {
    const count = html.split(p).length - 1;
    if (count !== 1) problems.push(`placeholder ${p} appears ${count} times`);
  }
  const size = Buffer.byteLength(html);
  if (size > BUDGET)
    problems.push(`template is ${size} bytes, over the ${BUDGET} byte budget`);
  if (problems.length) {
    console.error(problems.join("\n"));
    process.exit(1);
  }
  const notices = licenses();
  if (check) {
    const same =
      existsSync(TEMPLATE) &&
      readFileSync(TEMPLATE, "utf8") === html &&
      existsSync(LICENSES) &&
      readFileSync(LICENSES, "utf8") === notices;
    if (!same) {
      console.error(
        "The committed report template is stale. Run: npm run build:report",
      );
      process.exit(1);
    }
    console.log(`report template is fresh (${(size / 1024).toFixed(0)} KB)`);
    return;
  }
  writeFileSync(TEMPLATE, html);
  writeFileSync(LICENSES, notices);
  console.log(
    `wrote ${relative(repo, TEMPLATE)} (${(size / 1024).toFixed(0)} KB)`,
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1])
  main();
