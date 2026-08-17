import { describe, expect, it } from "vitest";
import { computeDisplayMode, computeDisplayWindow } from "./recency-display-mode";

const ONE_DAY_MS = 24 * 60 * 60_000;

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
    expect(window.end - window.start).toBe(7 * ONE_DAY_MS);
  });

  it("uses an elapsed week without pretending weekends or holidays are trading sessions", () => {
    const monday = Date.UTC(2026, 0, 5, 14, 0, 0);
    const window = computeDisplayWindow("all-symbols", monday);

    const expectedStart = Date.UTC(2025, 11, 29, 14, 0, 0);
    expect(window.start).toBe(expectedStart);
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
