// The page's globals, set in one place (refactor P2). Inline handlers and innerHTML templates call these by name, and
// window.LS is a read-only console handle for debugging (CLAUDE.md: render synthetic turns into LS.current).
import * as main from "./main.mjs";

window.showView = (...args) => main.showView(...args);
window.LS = new Proxy({}, {
  get: (_, k) => [main].map((m) => m[k]).find((v) => v !== undefined),
});
