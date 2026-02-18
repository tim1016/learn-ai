// jsdom lacks ResizeObserver — provide a no-op stub
(globalThis as any).ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom lacks HTMLCanvasElement.getContext — stub it so lightweight-charts
// doesn't crash when loaded without a vi.mock override
HTMLCanvasElement.prototype.getContext = (() => null) as any;
