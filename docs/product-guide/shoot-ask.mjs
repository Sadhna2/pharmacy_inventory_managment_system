/**
 * Third pass: the Ask screen — a question typed, answered, and its SQL opened.
 *
 * Same CDP harness as shoot.mjs. Two shots rather than one, because the claim
 * this feature makes is not "it answers" but "you can check the answer", and
 * only the second shot shows that.
 *
 *   SEED_PASSWORD=... APP=http://localhost:8090 node shoot-ask.mjs ./shots
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const OUT = process.argv[2] || "./shots";
const APP = process.env.APP || "http://localhost:5173";
const EMAIL = "admin@pharmacy.co.in";
const PASSWORD = process.env.SEED_PASSWORD;
if (!PASSWORD) {
  console.error("Set SEED_PASSWORD to the value you seeded the database with.");
  process.exit(1);
}
const PORT = 9336;

mkdirSync(OUT, { recursive: true });

const chrome = spawn(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  [
    `--remote-debugging-port=${PORT}`,
    "--headless=new",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--ignore-certificate-errors",
    `--user-data-dir=${OUT}/.profile-ask`,
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
  await sleep(1500);
};

async function shoot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "webp", quality: 62 });
  writeFileSync(`${OUT}/${name}.webp`, Buffer.from(data, "base64"));
  console.log(`${name.padEnd(22)} ${(data.length / 1365).toFixed(0)}KB`);
}

await go("/");
await sleep(2500);
// React tracks input state on the node, so setting `.value` directly is
// invisible to it. Going through the prototype setter and dispatching the
// event is what makes the form see a real keystroke.
await evaluate(`(() => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const fill = (el, v) => { set.call(el, v); el.dispatchEvent(new Event("input", { bubbles: true })); };
  fill(document.querySelector('input[type=email], input[name=email]'), ${JSON.stringify(EMAIL)});
  fill(document.querySelector('input[type=password]'), ${JSON.stringify(PASSWORD)});
  document.querySelector('form').requestSubmit();
})()`);
await sleep(5000);

const askOne = async (question) => {
  const typed = await evaluate(`(() => {
    const box = document.querySelector('textarea');
    if (!box) return "no textarea";
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    set.call(box, ${JSON.stringify(question)});
    box.dispatchEvent(new Event("input", { bubbles: true }));
    return "typed";
  })()`);
  console.log(`ask: ${typed}`);
  // The composer is not a <form> — Enter is handled on the textarea and the
  // send button has an onClick. React needs a tick to enable the button after
  // the input event, so the click comes after a beat.
  await sleep(400);
  const sent = await evaluate(`(() => {
    const btn = document.querySelector('button[aria-label="Ask"]');
    if (!btn) return "no send button";
    if (btn.disabled) return "still disabled";
    btn.click();
    return "sent";
  })()`);
  console.log(`send: ${sent}`);
  // The model call, then the query. Generous: a cold prefix cache is slow.
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    const done = await evaluate(
      `document.querySelectorAll('table, [data-answer]').length > 0`,
    );
    if (done) break;
  }
  await sleep(1200);
};

await go("/ask");
await sleep(2000);
await askOne("Which batches expire in the next 90 days and how many units are in them?");
await shoot("ask");

// Open the disclosure that carries the query, which is the whole argument.
const opened = await evaluate(`(() => {
  const d = [...document.querySelectorAll('details')].find(
    x => /show the sql/i.test(x.innerText));
  if (!d) return "no disclosure";
  d.open = true;
  d.scrollIntoView({ block: "center" });
  return "opened";
})()`);
console.log("sql disclosure:", opened);
await sleep(1200);
await shoot("ask-sql");

ws.close();
chrome.kill();
console.log("done");
process.exit(0);
