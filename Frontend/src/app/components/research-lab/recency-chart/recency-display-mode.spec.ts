import { describe, expect, it } from "vitest";
import { ALL_SYMBOLS_WINDOW_MS, computeDisplayMode, computeDisplayWindow } from "./recency-display-mode";

describe("computeDisplayMode", () => {
  it("is single-symbol when exactly one symbol is visible", () => {
    expect(computeDisplayMode(["SPY"])).toBe("single-symbol");
  });

  it("is all-symbols when more than one symbol is visible", () => {
    expect(computeDisplayMode(["SPY", "AAPL"])).toBe("all-symbols");
  });

  it("is all-symbols when zero symbols are visible", () => {
    expect(computeDisplayMode([])).toBe("all-symbols");
  });
});

describe("computeDisplayWindow", () => {
  const now = 1_000_000_000_000;

  it("caps the all-symbols window to roughly one trading week", () => {
    const window = computeDisplayWindow("all-symbols", now);
    expect(window.end).toBe(now);
    expect(window.end - window.start).toBe(ALL_SYMBOLS_WINDOW_MS);
  });

  it("uses the full accumulated history for single-symbol mode", () => {
    const window = computeDisplayWindow("single-symbol", now, { earliestEntryMs: now - 5_000_000 });
    expect(window.end).toBe(now);
    expect(window.start).toBe(now - 5_000_000);
  });

  it("single-symbol mode with no history yet still produces a valid (non-inverted) window", () => {
    const window = computeDisplayWindow("single-symbol", now, { earliestEntryMs: null });
    expect(window.start).toBeLessThan(window.end);
  });
});
