/**
 * Second pass: the detail panels — the screens where the system shows its
 * working. Same CDP harness as shoot.mjs.
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
const PORT = 9334;

mkdirSync(OUT, { recursive: true });

const chrome = spawn(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  [
    `--remote-debugging-port=${PORT}`,
    "--headless=new",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    `--user-data-dir=${OUT}/.profile2`,
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
    } catch {}
    await sleep(250);
  }
  throw new Error("Chrome never came up");
}

const ws = new WebSocket(await target());
await new Promise((r) => (ws.onopen = r));
let seq = 0;
const pending = new Map();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  const slot = pending.get(m.id);
  if (!slot) return;
  pending.delete(m.id);
  m.error ? slot.reject(new Error(JSON.stringify(m.error))) : slot.resolve(m.result);
};
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
const evaluate = async (expression) => {
  const { result } = await send("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true,
  });
  return result.value;
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1.5, mobile: false,
});

const go = async (path) => {
  await send("Page.navigate", { url: APP + path });
  await sleep(1200);
};

async function shoot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "webp", quality: 62 });
  writeFileSync(`${OUT}/${name}.webp`, Buffer.from(data, "base64"));
  console.log(`${name.padEnd(22)} ${(data.length / 1365).toFixed(0)}KB`);
}

// --- the login screen, before signing in ------------------------------------
await go("/");
await sleep(3000);
await shoot("login");

await evaluate(`(() => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const fill = (el, v) => { set.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); };
  fill(document.querySelector('input[type=email], input[name=email]'), ${JSON.stringify(EMAIL)});
  fill(document.querySelector('input[type=password]'), ${JSON.stringify(PASSWORD)});
  document.querySelector('form').requestSubmit();
})()`);
await sleep(5000);

/** Click the nth row of the first real data table on the page. */
const clickRow = (n = 0) => evaluate(`(() => {
  const rows = [...document.querySelectorAll('tbody tr')].filter(r => r.offsetHeight > 20);
  if (!rows[${n}]) return "no row";
  rows[${n}].click();
  return rows[${n}].innerText.slice(0, 40).replace(/\\n/g, " ");
})()`);

const panels = [
  { name: "replenishment-detail", path: "/replenishment", settle: 9000, row: 1 },
  { name: "forecast-detail", path: "/forecast", settle: 9000, row: 0 },
  { name: "exceptions-detail", path: "/exceptions", settle: 8000, row: 0 },
  { name: "lead-time-detail", path: "/lead-times", settle: 5000, row: 0 },
  { name: "product-detail", path: "/products", settle: 3500, row: 0 },
];

for (const panel of panels) {
  await go(panel.path);
  await sleep(panel.settle);
  const clicked = await clickRow(panel.row);
  await sleep(2500);
  console.log(`  opened: ${clicked}`);
  await shoot(panel.name);
}

// --- the collapsed rail, to show the layout adapts ---------------------------
await go("/stock");
await sleep(3000);
await evaluate(`(() => {
  const btn = [...document.querySelectorAll('button')].find(b => /collapse/i.test(b.innerText));
  if (btn) btn.click();
  return !!btn;
})()`);
await sleep(1200);
await shoot("stock-collapsed");

ws.close();
chrome.kill();
console.log("done");
process.exit(0);
