/**
 * Inline every screenshot into the guide so it can be published as a single
 * self-contained page.
 *
 *   node docs/product-guide/build.mjs
 *
 * `guide.template.html` is the source you edit. It references shots by name —
 * `src="{{dashboard}}"` — and this turns each one into a data: URI, so the
 * result depends on nothing: no network, no `shots/` folder beside it, no
 * second file of any kind. Download it, double-click it, read it.
 *
 * `PRODUCT-GUIDE.html` IS COMMITTED, unusually for a build output. Edit the
 * template, run this, commit both.
 *
 * THE NAMES ARE THE POINT. This used to be `guide.html` building into
 * `dist/guide.html`, and two files with one name is a trap: the readable one
 * was three directories down, the unreadable one sat at the obvious path, and
 * README linked to the unreadable one. Three separate people-including-me
 * downloaded the wrong file and concluded the screenshots were missing. They
 * were never missing — all twenty-five are in `shots/`, and the template
 * simply does not reference them until this script runs.
 *
 * So: nothing in this folder is called `guide.html` any more. The one file
 * that looks like the product guide is the product guide.
 */
import { readFileSync, writeFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SHOTS = join(HERE, "shots");
const SOURCE = join(HERE, "guide.template.html");
const DEST = join(HERE, "PRODUCT-GUIDE.html");

const images = Object.fromEntries(
  readdirSync(SHOTS)
    .filter((f) => f.endsWith(".webp"))
    .map((f) => [
      f.replace(/\.webp$/, ""),
      `data:image/webp;base64,${readFileSync(join(SHOTS, f)).toString("base64")}`,
    ]),
);

const template = readFileSync(SOURCE, "utf8");
let html = template;

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

const unused = Object.keys(images).filter((k) => !template.includes(`{{${k}}}`));
if (unused.length) console.warn(`Not referenced by the guide: ${unused.join(", ")}`);

writeFileSync(DEST, html);
console.log(`${DEST}  ${(html.length / 1048576).toFixed(2)} MB  ${Object.keys(images).length} images inlined`);
