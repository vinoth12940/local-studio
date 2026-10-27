// One store per concern (refactor P2). Names that more than one module assigns live in a store, because an ES module
// cannot assign a binding it imports. A store is a plain object whose property writes notify its subscribers; nothing
// subscribes yet, so rendering works exactly as before (the P2 split moves code, it does not redesign rendering).
export function createStore(initial) {
  const subs = new Set();
  const subscribe = (fn) => { subs.add(fn); return () => subs.delete(fn); };
  return new Proxy({ ...initial }, {
    get: (t, k) => (k === "subscribe" ? subscribe : t[k]),
    set: (t, k, v) => {
      const old = t[k];
      t[k] = v;
      if (old !== v) for (const fn of subs) fn(k, v, old);
      return true;
    },
  });
}
