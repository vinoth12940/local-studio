// Build guard: runs the UI's start-up code in a stub DOM so errors thrown at start-up (for example using a `const` before
// its declaration, or importing a name a module does not export) fail the build instead of shipping a blank app.
// The UI is index.html's module script (frontend/main.mjs and everything it imports) plus any inline classic <script>.
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const file = process.argv[2];
const h = fs.readFileSync(file, "utf8");
const inline = [...h.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const modules = [...h.matchAll(/<script type="module" src="([^"]+)"><\/script>/g)].map((m) => m[1]);
const TEXT = new Set(["value", "textContent", "innerHTML", "placeholder", "title", "className"]);
const el = () => new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => "" : TEXT.has(k) ? ""
    : k === "classList" ? { toggle() {}, add() {}, remove() {}, contains() { return false; } }
    : k === "style" || k === "dataset" ? {} : el(),
  set: () => true,
  apply: () => el(),
});
global.document = new Proxy({}, { get: (t, k) => (k === "documentElement" ? { dataset: {} } : el()) });
global.window = global;
global.localStorage = { getItem: () => null, setItem() {} };
global.matchMedia = () => ({ matches: false });
global.innerWidth = 1400;
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
global.requestAnimationFrame = () => 0;
global.navigator = { clipboard: {} };
global.crypto = { getRandomValues: (a) => a };
(async () => {
  if (!inline.length && !modules.length) throw new Error(`no UI script in ${file}`);
  for (const js of inline) new Function(js)();
  for (const src of modules) await import(pathToFileURL(path.resolve(path.dirname(file), src)).href);
  console.log("   UI script OK");
})().catch((e) => {
  console.error("UI script error at start-up:", e.message);
  process.exit(1);
});
