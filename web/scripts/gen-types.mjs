import { writeFileSync } from "node:fs";
import { TYPES, typesText } from "./types-lib.mjs";

writeFileSync(TYPES, await typesText());
console.log("wrote src/decision/types.ts");
