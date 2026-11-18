// Writes web/src/decision/tokens.json from index.css (--ba-*) and tokens.css (--tf-*).
// Python charts, notebooks and docs figures read that JSON. Run: npm run tokens
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { tokensText } from "./tokens-lib.mjs";

const web = fileURLToPath(new URL("..", import.meta.url));
export const SOURCES = ["src/index.css", "src/decision/tokens.css"];
const text = tokensText(SOURCES.map((f) => readFileSync(web + f, "utf8")));
writeFileSync(web + "src/decision/tokens.json", text);
console.log("wrote src/decision/tokens.json");
