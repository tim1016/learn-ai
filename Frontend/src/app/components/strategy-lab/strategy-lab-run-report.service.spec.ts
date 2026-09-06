import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { Observable, of, Subject, throwError } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BacktestRunsService } from "../../services/backtest-runs.service";
import type { BacktestRunDetail } from "../../services/backtest-runs.types";
import { makeTrade, curve, makeRun } from "./testing/run-fixtures";
import { PARITY_POLL_MS, StrategyLabRunReport, toEngineTrade } from "./strategy-lab-run-report.service";

/**
 * `runId: null` leaves the resource idle — that is the "no run selected"
 * state, and it must not reach the run service. Reading a signal is what
 * makes `rxResource` evaluate its params, so every test touches `report`
 * after awaiting a microtask.
 */
function makeReport(read: Observable<BacktestRunDetail | null>, runId: number | null) {
  const get = vi.fn(() => read);
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      StrategyLabRunReport,
      { provide: BacktestRunsService, useValue: { get } },
    ],
  });
  const report = TestBed.inject(StrategyLabRunReport);
  report.activeRunId.set(runId);
  // No ComponentFixture exists in this bare-injectable test, so nothing else
  // drives Angular's zoneless CD cycle. `rxResource`'s internal params-tracking
  // effect only runs once flushed explicitly.
  TestBed.tick();
  return { report, get };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("StrategyLabRunReport", () => {
  it("does not read until a run is active", async () => {
    const { report, get } = makeReport(of(null), null);
    await Promise.resolve();

    expect(report.run()).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it("uses the producer-authored realized staircase instead of mark-to-market points", async () => {
    const run = makeRun();
    const { report } = makeReport(of(run), run.id);
    await Promise.resolve();

    expect(report.equityPoints()).toEqual([
      { timeMs: Date.UTC(2026, 0, 5, 15, 0), value: 100_000 },
      { timeMs: makeTrade().exitTimestamp, value: 100_048 },
      { timeMs: Date.UTC(2026, 0, 6, 21, 0), value: 100_048 },
    ]);
  });

  it("keeps buy and sell markers tied to persisted trade outcomes", async () => {
    const run = makeRun({ trades: [makeTrade({ pnL: 0, pnlPts: 0, pnlPct: 0 })] });
    const { report } = makeReport(of(run), run.id);
    await Promise.resolve();

    expect(report.markers()).toEqual([
      expect.objectContaining({ color: "#90a4ae", text: "BUY · BREAK EVEN" }),
      expect.objectContaining({ color: "#90a4ae", text: "SELL · $0.00" }),
    ]);
  });

  it("makes a missing realized curve explicit instead of relabeling mark-to-market evidence", async () => {
    const run = makeRun({
      equityCurve: {
        schema_version: 2,
        mark_to_market: curve([{ t: 1, e: 100 }], "strategy_bar_close"),
        realized: { cadence: "trade_exit", downsample: { raw_points: 0, kept_points: 0 }, error: "Realized equity unavailable.", points: [] },
      },
    });
    const { report } = makeReport(of(run), run.id);
    await Promise.resolve();

    expect(report.reportNotices()).toContain("Realized equity unavailable.");
    expect(report.equityPoints()).toEqual([]);
  });

  it("hands the results components the analytics body of the frozen envelope", async () => {
    const analytics = { horizons: [], timing_cells: [], seasonality: [], rolling_trade_stability: [], sharpe_pnl_divergence: null };
    const run = makeRun({
      validationAnalytics: { schema_version: 2, computed_at_ms: 1, engine: "python", analytics },
    });
    const { report } = makeReport(of(run), run.id);
    await Promise.resolve();

    expect(report.engineResult()?.validation_analytics).toBe(analytics);
    expect(report.reportNotices()).not.toContain("Validation analytics were not recorded for this run.");
  });

  it("surfaces a malformed persisted verdict as a notice with no verdict", async () => {
    const run = makeRun({ verdictJson: "{}" });
    const { report } = makeReport(of(run), run.id);
    await Promise.resolve();

    expect(report.verdict()).toBeNull();
    expect(report.reportNotices()).toContain("Persisted verdict data is incomplete or malformed.");
  });

  it("does not mislabel a report-read failure as a missing run", async () => {
    const { report } = makeReport(throwError(() => new Error("Backtest detail read failed.")), 44);
    await Promise.resolve();

    expect(report.loadError()).toBeTruthy();
    expect(report.run()).toBeNull();
    expect(report.notFound()).toBe(false);
  });

  it("reports a run the server does not have as missing, not as a load failure", async () => {
    const { report } = makeReport(of(null), 404);
    await Promise.resolve();

    expect(report.notFound()).toBe(true);
    expect(report.loadError()).toBeUndefined();
  });

  it("does not call an unselected run missing", async () => {
    const { report } = makeReport(of(null), null);
    await Promise.resolve();

    expect(report.notFound()).toBe(false);
  });

  it("keeps the loaded run on screen until its replacement arrives", async () => {
    const first = makeRun({ id: 11 });
    const second = makeRun({ id: 12 });
    const replacement = new Subject<BacktestRunDetail | null>();
    const get = vi.fn((id: number) => (id === first.id ? of(first) : replacement.asObservable()));
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        StrategyLabRunReport,
        { provide: BacktestRunsService, useValue: { get } },
      ],
    });
    const report = TestBed.inject(StrategyLabRunReport);
    report.activeRunId.set(first.id);
    TestBed.tick();
    await Promise.resolve();
    expect(report.displayRun()?.id).toBe(first.id);

    // A re-run points the page at the new run before its read resolves.
    report.activeRunId.set(second.id);
    TestBed.tick();
    await Promise.resolve();

    // Nothing is destroyed before its replacement exists (spec §3.4): the
    // stage keeps rendering the previous run — with its own markers and
    // equity — while `run()` stays honest about nothing being loaded yet.
    expect(report.run()).toBeNull();
    expect(report.loading()).toBe(true);
    expect(report.notFound()).toBe(false);
    expect(report.displayRun()?.id).toBe(first.id);
    expect(report.equityPoints()).not.toEqual([]);

    replacement.next(second);
    TestBed.tick();
    await Promise.resolve();

    expect(report.displayRun()?.id).toBe(second.id);
  });

  it("drops the displayed run when the replacement turns out not to exist", async () => {
    const { report } = makeReport(of(null), 404);
    await Promise.resolve();

    expect(report.notFound()).toBe(true);
    expect(report.displayRun()).toBeNull();
  });

  it("re-reads on the poll cadence only while a parity verdict is pending", async () => {
    vi.useFakeTimers();
    const pending = makeRun({ parityVerdicts: [{ id: 1, status: "pending", verdictJson: "{}", createdAt: 1 }] });
    const settled = makeRun({ parityVerdicts: [{ id: 1, status: "agree", verdictJson: "{}", createdAt: 2 }] });
    const reads = [pending, settled, settled];
    const get = vi.fn(() => of(reads.shift() ?? settled));
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        StrategyLabRunReport,
        { provide: BacktestRunsService, useValue: { get } },
      ],
    });
    const report = TestBed.inject(StrategyLabRunReport);
    report.activeRunId.set(pending.id);
    TestBed.tick();
    await vi.advanceTimersByTimeAsync(0);
    expect(get).toHaveBeenCalledTimes(1);
    expect(report.parity()?.status).toBe("pending");

    await vi.advanceTimersByTimeAsync(PARITY_POLL_MS);
    expect(get).toHaveBeenCalledTimes(2);
    expect(report.parity()?.status).toBe("agree");

    // Settled: the poll has ended.
    await vi.advanceTimersByTimeAsync(PARITY_POLL_MS * 3);
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe("toEngineTrade", () => {
  it("passes through persisted P&L fields without recomputing them", () => {
    const trade = toEngineTrade(makeTrade({ pnlPts: 7, pnlPct: 0.02, pnL: -1.9 }), 4);
    expect(trade).toEqual(expect.objectContaining({ trade_number: 5, pnl_pts: 7, pnl_pct: 0.02, result: "LOSS" }));
  });
});
