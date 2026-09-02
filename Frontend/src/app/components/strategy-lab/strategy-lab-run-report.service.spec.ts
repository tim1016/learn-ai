import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { Apollo } from "apollo-angular";
import { from } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { makeTrade, curve, makeRun } from "./testing/run-fixtures";
import { StrategyLabRunReport, toEngineTrade } from "./strategy-lab-run-report.service";

/**
 * `runId: null` leaves the resource idle — that is the "no run selected"
 * state, and it must not reach Apollo. Reading a signal is what makes
 * `rxResource` evaluate its params, so every test touches `report` after
 * awaiting a microtask.
 */
function makeReport(queryResult: unknown, runId: number | null) {
  const stopPolling = vi.fn();
  const watchQuery = vi.fn(() => ({ valueChanges: from([queryResult]), stopPolling }));
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      StrategyLabRunReport,
      { provide: Apollo, useValue: { watchQuery } },
    ],
  });
  const report = TestBed.inject(StrategyLabRunReport);
  report.activeRunId.set(runId);
  // No ComponentFixture exists in this bare-injectable test, so nothing else
  // drives Angular's zoneless CD cycle. `rxResource`'s internal params-tracking
  // effect only runs once flushed explicitly.
  TestBed.tick();
  return { report, watchQuery, stopPolling };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe("StrategyLabRunReport", () => {
  it("does not query until a run is active", async () => {
    const { report, watchQuery } = makeReport({ data: { backtestRun: null }, loading: false }, null);
    await Promise.resolve();

    expect(report.run()).toBeNull();
    expect(watchQuery).not.toHaveBeenCalled();
  });

  it("uses the producer-authored realized staircase instead of mark-to-market points", async () => {
    const run = makeRun();
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.equityPoints()).toEqual([
      { timeMs: Date.UTC(2026, 0, 5, 15, 0), value: 100_000 },
      { timeMs: makeTrade().exitTimestamp, value: 100_048 },
      { timeMs: Date.UTC(2026, 0, 6, 21, 0), value: 100_048 },
    ]);
  });

  it("keeps buy and sell markers tied to persisted trade outcomes", async () => {
    const run = makeRun({ trades: [makeTrade({ pnL: 0, pnlPts: 0, pnlPct: 0 })] });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.markers()).toEqual([
      expect.objectContaining({ color: "#90a4ae", text: "BUY · BREAK EVEN" }),
      expect.objectContaining({ color: "#90a4ae", text: "SELL · $0.00" }),
    ]);
  });

  it("makes a missing realized curve explicit instead of relabeling mark-to-market evidence", async () => {
    const run = makeRun({
      equityCurve: {
        schemaVersion: 2,
        error: null,
        markToMarket: curve([{ t: 1, e: 100 }], "strategy_bar_close"),
        realized: { cadence: "trade_exit", rawPoints: 0, keptPoints: 0, error: "Realized equity unavailable.", points: [] },
      },
    });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.reportNotices()).toContain("Realized equity unavailable.");
    expect(report.equityPoints()).toEqual([]);
  });

  it("surfaces a malformed persisted verdict as a notice with no verdict", async () => {
    const run = makeRun({ verdictJson: "{}" });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.verdict()).toBeNull();
    expect(report.reportNotices()).toContain("Persisted verdict data is incomplete or malformed.");
  });

  it("does not mislabel a report-query failure as a missing run", async () => {
    const { report, stopPolling } = makeReport(
      { data: undefined, loading: false, error: new Error("Backtest detail query is incompatible with the server.") },
      44,
    );
    await Promise.resolve();

    expect(report.loadError()).toBeTruthy();
    expect(report.run()).toBeNull();
    expect(stopPolling).toHaveBeenCalled();
  });

  it("keeps polling only while a parity verdict is pending", async () => {
    const pending = makeRun({ parityVerdicts: [{ id: 1, status: "pending", verdictJson: "{}", createdAt: 1 }] });
    const { stopPolling } = makeReport({ data: { backtestRun: pending }, loading: false }, pending.id);
    await Promise.resolve();

    expect(stopPolling).not.toHaveBeenCalled();
  });
});

describe("toEngineTrade", () => {
  it("passes through persisted P&L fields without recomputing them", () => {
    const trade = toEngineTrade(makeTrade({ pnlPts: 7, pnlPct: 0.02, pnL: -1.9 }), 4);
    expect(trade).toEqual(expect.objectContaining({ trade_number: 5, pnl_pts: 7, pnl_pct: 0.02, result: "LOSS" }));
  });
});
