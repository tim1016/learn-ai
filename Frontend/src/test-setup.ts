// jsdom lacks ResizeObserver — provide a no-op stub
(globalThis as any).ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
