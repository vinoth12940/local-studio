// Build guard: runs index.html's script in a stub DOM so errors thrown at start-up (for example
// using a `const` before its declaration) fail the build instead of shipping a blank app.
const fs = require("fs");
const h = fs.readFileSync(process.argv[2], "utf8");
const js = h.slice(h.lastIndexOf("<script>") + 8, h.lastIndexOf("</script>"));
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
try {
  new Function(js)();
  console.log("   UI script OK");
} catch (e) {
  console.error("UI script error at start-up:", e.message);
  process.exit(1);
}
