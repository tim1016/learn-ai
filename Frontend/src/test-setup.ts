// jsdom lacks ResizeObserver — provide a no-op stub
(globalThis as any).ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom lacks Element.scrollIntoView — no-op so components that scroll a card
// into view (e.g. broker checklist "Fix this") don't crash under test.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

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

// jsdom's Web Storage can be shadowed by Node's own experimental global
// `localStorage`/`sessionStorage` (Node ≥22), which is undefined unless
// `--localstorage-file` is passed and takes precedence on globalThis. On a
// host Node that new, bare `localStorage` in a spec resolves to that undefined
// global and every setup `localStorage.clear()` throws. Install a minimal
// in-memory Storage only when the ambient one is unusable, so CI's pinned-Node
// jsdom storage is left untouched.
function installStorageIfMissing(key: 'localStorage' | 'sessionStorage'): void {
  const globalWithStorage = globalThis as unknown as Record<string, Storage | undefined>;
  try {
    const existing = globalWithStorage[key];
    if (existing) {
      existing.setItem('__probe__', '1');
      existing.removeItem('__probe__');
      return;
    }
  } catch {
    // Ambient storage exists but is unusable (Node's flag-gated global);
    // fall through and install the in-memory replacement below.
  }
  const store = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(itemKey: string): string | null {
      return store.has(itemKey) ? (store.get(itemKey) as string) : null;
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(itemKey: string): void {
      store.delete(itemKey);
    },
    setItem(itemKey: string, value: string): void {
      store.set(String(itemKey), String(value));
    },
  };
  Object.defineProperty(globalThis, key, { configurable: true, value: storage });
}

installStorageIfMissing('localStorage');
installStorageIfMissing('sessionStorage');
