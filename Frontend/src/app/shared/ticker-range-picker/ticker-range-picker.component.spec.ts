import { render, screen } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TickerRangePickerComponent } from "./ticker-range-picker.component";
import type { TickerOption, TickerRange } from "./ticker-range-picker.types";
import { computeAdvisories, summarizeAvailability } from "./ticker-range-picker.types";

const POOL: readonly TickerOption[] = [
  { symbol: "SPY", name: "SPDR S&P 500 ETF", exchange: "ARCA", cache: 1, last: "2026-04-23" },
  { symbol: "AAPL", name: "Apple Inc.", exchange: "NASDAQ", cache: 0.9, last: "2026-04-23" },
  { symbol: "PLTR", name: "Palantir", exchange: "NASDAQ", cache: 0, last: null },
];

function baseRange(): TickerRange {
  return { symbol: "SPY", from: "2026-03-24", to: "2026-04-23", resolution: "minute", autoFetch: false };
}

describe("TickerRangePickerComponent", () => {
  it("renders the selected symbol in the collapsed combobox", async () => {
    await render(TickerRangePickerComponent, {
      inputs: { value: baseRange(), tickerPool: POOL, recent: ["SPY"] },
    });
    expect(screen.getByText("SPY")).toBeTruthy();
  });

  it("opens the dropdown on click and lists recent then all tickers", async () => {
    const user = userEvent.setup();
    await render(TickerRangePickerComponent, {
      inputs: { value: baseRange(), tickerPool: POOL, recent: ["SPY", "AAPL"] },
    });

    await user.click(screen.getByText("SPY"));

    expect(screen.getByText("Recent")).toBeTruthy();
    expect(screen.getByText("All tracked instruments")).toBeTruthy();
    expect(screen.getAllByText("AAPL").length).toBeGreaterThan(0);
  });

  it("renders the 1m/1h/1d resolution tri-toggle", async () => {
    await render(TickerRangePickerComponent, {
      inputs: { value: baseRange(), tickerPool: POOL },
    });
    expect(screen.getByRole("button", { name: "1m" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "1h" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "1d" })).toBeTruthy();
  });
});

describe("summarizeAvailability", () => {
  it("counts cells by status and ignores weekends", () => {
    const s = summarizeAvailability([
      { date: "2026-01-01", status: "complete" },
      { date: "2026-01-02", status: "complete" },
      { date: "2026-01-03", status: "weekend" },
      { date: "2026-01-04", status: "weekend" },
      { date: "2026-01-05", status: "partial" },
      { date: "2026-01-06", status: "hole" },
      { date: "2026-01-07", status: "missing" },
    ]);
    expect(s).toEqual({ complete: 2, partial: 1, hole: 1, missing: 1, weekdays: 5 });
  });
});

describe("computeAdvisories", () => {
  it("suggests hour bars when minute range > 90 days", () => {
    const advisories = computeAdvisories(
      { symbol: "SPY", from: "2025-01-01", to: "2026-04-01", resolution: "minute" },
      { complete: 100, partial: 0, hole: 0, missing: 0, weekdays: 100 }
    );
    expect(advisories.some((a) => a.kind === "suggest" && a.action?.patch?.resolution === "hour")).toBe(true);
  });

  it("warns when data is missing and auto-fetch is off", () => {
    const advisories = computeAdvisories(
      { symbol: "SPY", from: "2026-04-01", to: "2026-04-23", resolution: "daily", autoFetch: false },
      { complete: 5, partial: 0, hole: 0, missing: 3, weekdays: 8 }
    );
    expect(advisories.some((a) => a.kind === "warn" && a.action?.triggerRun)).toBe(true);
  });

  it("bad-marks a minute range > 365 days", () => {
    const advisories = computeAdvisories(
      { symbol: "SPY", from: "2024-01-01", to: "2026-04-01", resolution: "minute" },
      { complete: 500, partial: 0, hole: 0, missing: 0, weekdays: 500 }
    );
    expect(advisories.some((a) => a.kind === "bad")).toBe(true);
  });

  it("emits no advisories in the happy path", () => {
    const advisories = computeAdvisories(
      { symbol: "SPY", from: "2026-04-01", to: "2026-04-23", resolution: "daily", autoFetch: false },
      { complete: 17, partial: 0, hole: 0, missing: 0, weekdays: 17 }
    );
    expect(advisories).toEqual([]);
  });
});
