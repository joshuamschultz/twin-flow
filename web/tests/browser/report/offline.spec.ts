import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { fillTemplate, GOLDEN, readJson, TEMPLATE } from "./fill";

test("the offline report opens from file:// with the network blocked", async ({
  page,
}) => {
  const file = fillTemplate(readJson(GOLDEN), "offline.html");
  const blocked: string[] = [];
  const errors: string[] = [];
  await page.context().route("**", (route) => {
    const url = route.request().url();
    if (url.startsWith("file:") || url.startsWith("data:"))
      return route.continue();
    blocked.push(url);
    return route.abort();
  });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto(pathToFileURL(file).href);
  await expect(page.locator("[data-view='race']")).toBeVisible();
  expect(blocked).toEqual([]);
  expect(errors).toEqual([]);
});

test("the template holds no network URL outside XML namespace names", () => {
  const html = readFileSync(TEMPLATE, "utf8");
  const ns =
    /^http:\/\/www\.w3\.org\/(1999\/xhtml|1999\/xlink|2000\/svg|1998\/Math\/MathML|XML\/1998\/namespace|2000\/xmlns\/)/;
  const urls = [...html.matchAll(/https?:\/\/[^\s"'`<>)\\]*/g)].map(
    (m) => m[0],
  );
  expect(urls.filter((u) => !ns.test(u))).toEqual([]);
  expect(html).not.toMatch(/\b(?:src|href)\s*=\s*["']?(?:https?:)?\/\//i);
  expect(Buffer.byteLength(html)).toBeLessThan(500 * 1024);
});
