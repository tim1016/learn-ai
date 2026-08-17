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
    // Loosely "about a week" — exact span depends on weekday (see the
    // Monday-specific test below for the precise weekend-skipping check).
    expect(window.end - window.start).toBeGreaterThanOrEqual(4 * ONE_DAY_MS);
    expect(window.end - window.start).toBeLessThanOrEqual(9 * ONE_DAY_MS);
  });

  it("on a Monday, spans the prior weekend instead of landing on Sunday morning", () => {
    // 2026-01-05 is a Monday. The old bug (now - 32.5 elapsed hours)
    // would land on Sunday ~05:30 and cover only Monday's own session.
    // Five ACTUAL trading sessions back from Monday are: Mon, Fri, Thu,
    // Wed, Tue of the prior week — i.e. Tuesday 2025-12-30, six calendar
    // days earlier, skipping the intervening Sat/Sun entirely.
    const monday = Date.UTC(2026, 0, 5, 14, 0, 0);
    const window = computeDisplayWindow("all-symbols", monday);

    const expectedStart = Date.UTC(2025, 11, 30, 14, 0, 0);
    expect(window.start).toBe(expectedStart);
    expect(new Date(window.start).getUTCDay()).not.toBe(0); // not Sunday
    expect(new Date(window.start).getUTCDay()).not.toBe(6); // not Saturday
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
