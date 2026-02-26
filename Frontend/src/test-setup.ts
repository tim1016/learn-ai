// jsdom lacks ResizeObserver — provide a no-op stub
(globalThis as any).ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom lacks HTMLCanvasElement.getContext — stub it so lightweight-charts
// doesn't crash when loaded without a vi.mock override
HTMLCanvasElement.prototype.getContext = (() => null) as any;

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
