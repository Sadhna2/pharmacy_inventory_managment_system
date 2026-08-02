/**
 * Screenshot every screen of the app with headless Chrome over CDP.
 * No dependencies — Node 23 has a built-in WebSocket client.
 *
 *   node shoot.mjs <outDir>
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const OUT = process.argv[2] || "./shots";
const APP = "http://localhost:5173";
const EMAIL = "admin@pharmacy.co.in";
const PASSWORD = process.env.SEED_PASSWORD;
if (!PASSWORD) {
  // No default. A fallback here would be a working credential committed to the
  // repository, and the screenshots are worthless against a database you did
  // not seed anyway.
  console.error("Set SEED_PASSWORD to the value you seeded the database with.");
  process.exit(1);
}
const PORT = 9333;

const SHOTS = [
  { file: "dashboard", path: "/", wait: 3500 },
  { file: "products", path: "/products", wait: 3000 },
  { file: "stock", path: "/stock", wait: 3000 },
  { file: "movements", path: "/movements", wait: 3000 },
  { file: "purchasing", path: "/purchase-orders", wait: 3000 },
  { file: "sales", path: "/sales-orders", wait: 3000 },
  { file: "transfers", path: "/transfers", wait: 3000 },
  { file: "adjustments", path: "/adjustments", wait: 3000 },
  { file: "replenishment", path: "/replenishment", wait: 9000 },
  { file: "forecast", path: "/forecast", wait: 9000 },
  { file: "exceptions", path: "/exceptions", wait: 8000 },
  { file: "lead-times", path: "/lead-times", wait: 5000 },
  { file: "recalls", path: "/recalls", wait: 3000 },
  { file: "audit", path: "/audit", wait: 3000 },
  { file: "master-data", path: "/master-data", wait: 3000 },
  { file: "users", path: "/users", wait: 3000 },
  { file: "settings", path: "/settings", wait: 3500 },
];

mkdirSync(OUT, { recursive: true });

const chrome = spawn(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  [
    `--remote-debugging-port=${PORT}`,
    "--headless=new",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    `--user-data-dir=${OUT}/.profile`,
    "--window-size=1440,900",
    "about:blank",
  ],
  { stdio: "ignore" },
);

process.on("exit", () => chrome.kill());

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      /* not up yet */
    }
    await sleep(250);
  }
  throw new Error("Chrome never came up");
}

const ws = new WebSocket(await target());
await new Promise((r) => (ws.onopen = r));

let seq = 0;
const pending = new Map();
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  const slot = pending.get(msg.id);
  if (!slot) return;
  pending.delete(msg.id);
  msg.error ? slot.reject(new Error(JSON.stringify(msg.error))) : slot.resolve(msg.result);
};

const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

const evaluate = async (expression) => {
  const { result } = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  return result.value;
};

await send("Page.enable");
await send("Runtime.enable");
// deviceScaleFactor 2 so the downscaled image in the doc stays crisp.
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 900,
  deviceScaleFactor: 1.25,
  mobile: false,
});

async function go(path) {
  await send("Page.navigate", { url: APP + path });
  await sleep(1200);
}

// --- sign in ---------------------------------------------------------------
await go("/");
await sleep(2500);

await evaluate(`(() => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const fill = (el, v) => { set.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); };
  const email = document.querySelector('input[type=email], input[name=email]');
  const pass = document.querySelector('input[type=password]');
  if (!email || !pass) return "no form";
  fill(email, ${JSON.stringify(EMAIL)});
  fill(pass, ${JSON.stringify(PASSWORD)});
  document.querySelector('form').requestSubmit();
  return "submitted";
})()`);

await sleep(5000);
const who = await evaluate(`location.pathname + " :: " + document.body.innerText.slice(0, 60).replace(/\\n/g, " ")`);
console.log("after login →", who);

// --- capture ---------------------------------------------------------------
for (const shot of SHOTS) {
  await go(shot.path);
  await sleep(shot.wait);
  // Park the pointer off-canvas so no row sits in a hover state.
  await evaluate(`window.scrollTo(0, 0); document.body.style.cursor = "default"; 1`);
  const { data } = await send("Page.captureScreenshot", {
    format: "webp", quality: 54,
    captureBeyondViewport: false,
  });
  writeFileSync(`${OUT}/${shot.file}.webp`, Buffer.from(data, "base64"));
  const heading = await evaluate(
    `(document.querySelector("h1, h2")?.innerText || "").slice(0, 40)`,
  );
  console.log(`${shot.file.padEnd(16)} ${String(heading).padEnd(28)} ${(data.length / 1365).toFixed(0)}KB`);
}

ws.close();
chrome.kill();
console.log("done");
process.exit(0);
