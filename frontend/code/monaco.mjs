// code/monaco.mjs: Monaco, the editor VS Code is built on, loaded on first use from the copy bundled in
// frontend/vendor/monaco/ (VENDOR.json lists every file with its hash). The stylesheet, the AMD loader and the editor are
// pinned here; the loader then fetches the language colourers and the diff worker from the same bundled folder.
import { vendorLoad } from "../core/util.mjs";

const MONACO_FILES = [
  ["link", "frontend/vendor/monaco/vs/editor/editor.main.css", "sha384-06yHXpYRlHEPaR4AS0fB/W+lMN09Zh5e1XMtfkNQdHV38OlhfkOEW5M+pCj3QskC"],
  ["script", "frontend/vendor/monaco/vs/loader.js", "sha384-pHG02SG8pId94Np3AbPmBEJ1yPqaH0IkJGLSNGXYmuGhkazT8Lr/57WYpbkGjJtu"],
  ["script", "frontend/vendor/monaco/vs/editor/editor.main.js", "sha384-fj9z+NUc93I3woCCy5IRQfrQ8Amu1E27tllwgb5gz3d9Vr1ymS13xcF6two3e4KH"]];
const MONACO_BASE = "frontend/vendor/monaco/vs";
let monacoLoad = null;

function monacoTag(kind, url, sri) {
  return new Promise((ok, fail) => {
    const el = document.createElement(kind);
    if (kind === "link") { el.rel = "stylesheet"; el.href = url; el.dataset.name = "vs/editor/editor.main"; } else el.src = url;   // the loader's css! plugin finds the link by its name
    el.integrity = sri; el.crossOrigin = "anonymous"; el.onload = ok;
    el.onerror = () => fail(new Error("The editor could not load its Monaco files. Reinstall Local Studio to restore them."));
    document.head.appendChild(el);
  });
}

function loadMonaco() {
  monacoLoad ||= vendorLoad(async () => {
    const [css, loader, main] = MONACO_FILES;
    await monacoTag(...css);
    await monacoTag(...loader);
    window.require.config({ paths: { vs: new URL(MONACO_BASE, document.baseURI).href } });   // absolute: the language workers resolve it on their own
    await monacoTag(...main);
    const monaco = await new Promise((ok, fail) => window.require(["vs/editor/editor.main"], () => ok(window.monaco), fail));
    refreshMonacoTheme();
    return monaco;
  }).catch((err) => { monacoLoad = null; throw err; });
  return monacoLoad;
}

// The app's own palette, light or dark: the editor sits on --code-bg like the terminal. Called again by applyTheme; it
// reads window.monaco, so it is safe at start-up, before anything has loaded.
function refreshMonacoTheme() {
  const monaco = window.monaco;
  if (!monaco?.editor) return;
  const cs = getComputedStyle(document.documentElement), v = (n) => cs.getPropertyValue(n).trim(), dark = cs.colorScheme === "dark";
  monaco.editor.defineTheme("local-studio", {
    base: dark ? "vs-dark" : "vs", inherit: true, rules: [],
    colors: {
      "editor.background": v("--code-bg") || v("--bg"), "editorGutter.background": v("--code-bg") || v("--bg"),
      "editor.lineHighlightBackground": dark ? "#2f2e2b" : "#f3f1ea", "editorLineNumber.foreground": v("--muted"),
      "editorLineNumber.activeForeground": v("--ink"), "editorCursor.foreground": v("--brand"),
      "diffEditor.insertedTextBackground": dark ? "#5aae7a33" : "#2e7d4f22", "diffEditor.removedTextBackground": dark ? "#e5675a33" : "#c2412d22"
    }
  });
  monaco.editor.setTheme("local-studio");
}

export { loadMonaco, MONACO_FILES, refreshMonacoTheme };
