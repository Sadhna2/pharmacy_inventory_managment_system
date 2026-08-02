/**
 * Inline every screenshot into the guide so it can be published as a single
 * self-contained page.
 *
 *   node docs/product-guide/build.mjs
 *
 * `guide.html` is the source you edit. It references shots by name —
 * `src="{{dashboard}}"` — and this turns each one into a data: URI, because a
 * published artifact cannot fetch anything from another host.
 */
import { readFileSync, writeFileSync, readdirSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, "shots");
const OUT = join(HERE, "dist");

mkdirSync(OUT, { recursive: true });

const images = Object.fromEntries(
  readdirSync(SHOTS)
    .filter((f) => f.endsWith(".webp"))
    .map((f) => [
      f.replace(/\.webp$/, ""),
      `data:image/webp;base64,${readFileSync(join(SHOTS, f)).toString("base64")}`,
    ]),
);

let html = readFileSync(join(HERE, "guide.html"), "utf8");

const missing = new Set();
html = html.replace(/\{\{([a-z0-9-]+)\}\}/g, (whole, name) => {
  if (!images[name]) {
    missing.add(name);
    return whole;
  }
  return images[name];
});

if (missing.size) {
  console.error(`Missing screenshots: ${[...missing].join(", ")}`);
  process.exit(1);
}

const unused = Object.keys(images).filter((k) => !readFileSync(join(HERE, "guide.html"), "utf8").includes(`{{${k}}}`));
if (unused.length) console.warn(`Not referenced by the guide: ${unused.join(", ")}`);

const dest = join(OUT, "guide.html");
writeFileSync(dest, html);
console.log(`${dest}  ${(html.length / 1048576).toFixed(2)} MB  ${Object.keys(images).length} images inlined`);
