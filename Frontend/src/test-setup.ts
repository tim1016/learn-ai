// jsdom lacks ResizeObserver — provide a no-op stub
(globalThis as any).ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom lacks HTMLCanvasElement.getContext — return a no-op 2d-context shim
// so canvas-using components (edge-charts) can be instantiated without
// crashing. lightweight-charts is already module-mocked in
// /testing/mocks/lightweight-charts.mock.ts, so the upgrade from `() => null`
// is safe for it. Any new ctx method shows up at runtime as
// `ctx.foo is not a function`; the Proxy's catch-all `get` covers them.
HTMLCanvasElement.prototype.getContext = ((kind: string) => {
  if (kind !== "2d") return null;
  return new Proxy({}, {
    get: (_t, prop) => {
      if (prop === "canvas") return null;
      // measureText is the only method whose return value is read for a
      // property (`metrics.width`); everything else is fire-and-forget.
      if (prop === "measureText") return () => ({ width: 0 });
      return () => undefined;
    },
    set: () => true,
  });
}) as any;

// jsdom lacks window.matchMedia — stub it so PrimeNG Menubar
// doesn't crash during tests
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
