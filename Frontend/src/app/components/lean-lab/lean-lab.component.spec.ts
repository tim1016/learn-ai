import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";

// lightweight-charts pulled in transitively via LeanLabEquityChartComponent.
// We don't render the chart in these integration tests, but the import
// still resolves through the worker's module cache — and if the
// equity-chart spec runs in the same worker AFTER this one, its own
// vi.mock arrives too late and its 5 tests cascade-fail with "vi.fn()
// called 0 times" because the real lightweight-charts has been cached.
// Mocking here (identical stub shape to the chart spec) keeps the worker's
// cache mock-flavored regardless of test-file ordering.
vi.mock("lightweight-charts", () => {
  const series = { setData: vi.fn(), applyOptions: vi.fn() };
  const chart = {
    addSeries: vi.fn().mockReturnValue(series),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
  return { createChart: vi.fn().mockReturnValue(chart), CandlestickSeries: "CandlestickSeries" };
});

import { LeanSidecarApiError, LeanSidecarService } from "../../services/lean-sidecar.service";
import type {
  NormalizedResult,
  RunIndexResponse,
  RunReconciliationReport,
  RunSummary,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { LeanLabComponent } from "./lean-lab.component";

/**
 * Component-level tests. Asserts what the operator sees on the page,
 * not the internal signal values. The service is faked at the DI
 * level so these tests run without an HTTP boundary or a real
 * launcher.
 */

interface FakeLeanSidecarService {
  startTrustedRun: ReturnType<typeof vi.fn>;
  getNormalized: ReturnType<typeof vi.fn>;
  getManifest: ReturnType<typeof vi.fn>;
  getLogTail: ReturnType<typeof vi.fn>;
  getObservationsCsv: ReturnType<typeof vi.fn>;
  listRuns: ReturnType<typeof vi.fn>;
  reconcileRun: ReturnType<typeof vi.fn>;
}

function makeRunSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "ui_run_history_1",
    symbol: "SPY",
    requested_start_ms_utc: 1_736_121_600_000,
    requested_end_ms_utc: 1_736_467_200_000,
    started_at_ms: 1_736_121_650_000,
    finished_at_ms: 1_736_121_700_000,
    exit_code: 0,
    algorithm_source_kind: "trusted_sample",
    exit_clean: true,
    is_clean: true,
    ...overrides,
  };
}

function makeRunIndex(overrides: Partial<RunIndexResponse> = {}): RunIndexResponse {
  return { runs: [], cap: 200, truncated: false, ...overrides };
}

function makeResponse(overrides: Partial<TrustedRunResponse> = {}): TrustedRunResponse {
  return {
    run_id: "ui_run_20260517000000",
    is_clean: true,
    exit_code: 0,
    duration_ms: 1234,
    timed_out: false,
    lean_errors: {
      analysis_failed: [],
      failed_data_requests: [],
      runtime_error: [],
      other: [],
    },
    log_tail: "LEAN ALGORITHMIC TRADING ENGINE v2.5.0.0\n",
    manifest_path: "/tmp/ws/manifest.json",
    workspace_root: "/tmp/ws",
    observations_path: "/tmp/ws/workspace/output/storage/observations.csv",
    lean_log_path: "/tmp/ws/workspace/output/log.txt",
    normalized_path: "/tmp/ws/normalized/result.json",
    normalized_parser_version: "phase-3a-r1",
    total_order_events: 2,
    total_equity_points: 30,
    ...overrides,
  };
}

function makeNormalized(): NormalizedResult {
  return {
    parser_version: "phase-3a-r1",
    algorithm_id: "MyAlgorithm",
    statistics: { "Total Orders": "1", "Sharpe Ratio": "0" },
    runtime_statistics: {},
    equity_curve: [
      { ms_utc: 1_736_121_600_000, value: 100_000, open: 100_000, high: 100_000, low: 100_000 },
      { ms_utc: 1_736_467_200_000, value: 100_284.14, open: 100_284.14, high: 100_284.14, low: 100_284.14 },
    ],
    order_events: [],
    total_order_events: 2,
    total_equity_points: 30,
    first_equity_ms_utc: 1_736_121_600_000,
    last_equity_ms_utc: 1_736_467_200_000,
  };
}

describe("LeanLabComponent", () => {
  let fixture: ComponentFixture<LeanLabComponent>;
  let component: LeanLabComponent;
  let serviceMock: FakeLeanSidecarService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn(),
      // Phase 4e: default getManifest to reject with a 404 so tests
      // that don't care about rehydration don't accidentally exercise
      // it. loadRun swallows 404s as expected (legacy run case).
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      // Default listRuns to an empty index so the constructor's
      // refreshRuns() call doesn't throw in tests that don't set
      // it explicitly.
      listRuns: vi.fn().mockResolvedValue(makeRunIndex()),
      // Default reconcileRun rejects with a 404 so tests that don't
      // exercise the Phase 5a UI don't silently call into the real
      // endpoint shape; only the dedicated reconcile tests override.
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it("renders the form heading and defaults", () => {
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector("h1")?.textContent).toContain("LEAN Sidecar Lab");
    expect(component.form.controls.symbol.value).toBe("SPY");
    expect(component.form.controls.startingCash.value).toBe(100_000);
  });

  it("shows the clean-run badge + summary after a successful submit", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Clean run");
    expect(text).toContain("phase-3a-r1");
    expect(text).toContain("Equity snapshot");
    // P&L line shows when normalized result is loaded.
    expect(text).toContain("100,000.00");
  });

  it("renders the yellow LEAN-errors badge when is_clean is false but exit==0", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(
      makeResponse({
        is_clean: false,
        lean_errors: {
          analysis_failed: [],
          failed_data_requests: ["File not found: /lean-run/data/equity/usa/minute/spy/20250106_quote.zip"],
          runtime_error: [],
          other: [],
        },
      }),
    );

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("LEAN errors logged");
    expect(text).toContain("failed_data_requests");
    expect(text).toContain("quote.zip");
  });

  it("toggle hidden by default → request omits algorithm_source", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    await component.submit();
    const req = serviceMock.startTrustedRun.mock.calls[0][0];
    expect(req.algorithm_source).toBeUndefined();
  });

  it("toggle on + custom source → request carries algorithm_source", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    component.form.patchValue({
      useCustomAlgorithm: true,
      algorithmSource: "class MyAlgorithm: pass",
    });
    fixture.detectChanges();
    await component.submit();
    const req = serviceMock.startTrustedRun.mock.calls[0][0];
    expect(req.algorithm_source).toBe("class MyAlgorithm: pass");
  });

  it("toggle on + whitespace-only source → algorithm_source omitted (server fallback)", async () => {
    // The server 422s on whitespace-only algorithm_source. Better to
    // omit the field client-side and let the server quietly fall
    // back to the trusted sample.
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    component.form.patchValue({
      useCustomAlgorithm: true,
      algorithmSource: "   \n\t  ",
    });
    fixture.detectChanges();
    await component.submit();
    const req = serviceMock.startTrustedRun.mock.calls[0][0];
    expect(req.algorithm_source).toBeUndefined();
  });

  it("regenerates a unique runId on every successful submit", async () => {
    // Reviewer P1: two fast successful submits must not produce the
    // same runId (same-second collision would mix server-side
    // workspace artifacts). Seconds + milliseconds + 5-char random
    // suffix removes the collision class entirely.
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse());
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());

    const initial = component.form.controls.runId.value;
    await component.submit();
    const after1 = component.form.controls.runId.value;
    await component.submit();
    const after2 = component.form.controls.runId.value;

    // Each submit must have regenerated the id.
    expect(after1).not.toBe(initial);
    expect(after2).not.toBe(after1);
    // And the random suffix should match the slug constraint.
    expect(after2).toMatch(/^ui_run_\d{17}_[a-z0-9]{5}$/);
  });

  it("loads the run history on init and renders rows", async () => {
    // The constructor calls refreshRuns(), but our beforeEach() already
    // ran that with an empty index. Rebuild with a populated index.
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn(),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi.fn().mockResolvedValue(
        makeRunIndex({
          runs: [
            makeRunSummary({ run_id: "ui_run_a", symbol: "AAPL" }),
            makeRunSummary({ run_id: "ui_run_b", exit_clean: false, exit_code: 137 }),
          ],
        }),
      ),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    // Let the init-time listRuns promise settle.
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("ui_run_a");
    expect(text).toContain("ui_run_b");
    expect(text).toContain("AAPL");
  });

  it("re-fetches the run history after a successful submit", async () => {
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse({ run_id: "ui_run_new" }));
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());

    await component.submit();
    await fixture.whenStable();

    // 1 on init + 1 after the submit's finally block.
    expect(serviceMock.listRuns).toHaveBeenCalledTimes(2);
  });

  it("loadRun fetches the normalized result and renders the equity snapshot", async () => {
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    await component.loadRun("ui_run_history_42");
    fixture.detectChanges();

    expect(serviceMock.getNormalized).toHaveBeenCalledWith("ui_run_history_42");
    expect(component.response()?.run_id).toBe("ui_run_history_42");
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Equity snapshot");
  });

  it("loadRun preserves the historical run's failed exit state (reviewer P1)", async () => {
    // Seed the sidebar with a run that exited 137 (OOM) — exit_clean=false.
    // Then click it. The synthesized TrustedRunResponse MUST carry that
    // exit_code + is_clean=false, otherwise the status badge shows a
    // false-green "Clean run" pill.
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn().mockResolvedValue(makeNormalized()),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi.fn().mockResolvedValue(
        makeRunIndex({
          runs: [
            makeRunSummary({
              run_id: "ui_run_oom",
              exit_code: 137,
              exit_clean: false,
              is_clean: false,
            }),
          ],
        }),
      ),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();

    await component.loadRun("ui_run_oom");
    fixture.detectChanges();

    expect(component.response()?.is_clean).toBe(false);
    expect(component.response()?.exit_code).toBe(137);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).not.toContain("Clean run");
    expect(text).toContain("Exit 137");
  });

  it("loadRun does not paint a 'Clean run' badge when manifest is_clean=false despite exit_code=0", async () => {
    // Reviewer P1: a run that exited 0 but had classified LEAN errors
    // (failed_data_requests, runtime_error, etc.) has ``is_clean=false``
    // on the launcher response and in the manifest's ``is_clean=False``
    // note. The sidebar rehydration must branch on the manifest's
    // ``is_clean`` field — not on ``exit_clean`` (which is just
    // ``exit_code == 0`` and would paint this dirty run as green).
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn().mockResolvedValue(makeNormalized()),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi.fn().mockResolvedValue(
        makeRunIndex({
          runs: [
            makeRunSummary({
              run_id: "ui_run_dirty_zero_exit",
              exit_code: 0,
              exit_clean: true,
              is_clean: false,
            }),
          ],
        }),
      ),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();

    await component.loadRun("ui_run_dirty_zero_exit");
    fixture.detectChanges();

    // Synthesized response carries is_clean=false from the manifest.
    expect(component.response()?.is_clean).toBe(false);
    expect(component.response()?.exit_code).toBe(0);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    // Badge must NOT say "Clean run" — exit==0 alone doesn't qualify.
    // The component shows "LEAN errors logged" for exit==0 + not clean.
    expect(text).not.toContain("Clean run");
    expect(text).toContain("LEAN errors logged");
  });

  it("loadRun falls back to is_clean=false when manifest note is null (legacy run)", async () => {
    // Pre-Phase-2a manifests don't carry the ``is_clean`` note —
    // ``summary.is_clean`` arrives as null. Per the reviewer's
    // direction: fall back to false, never silently paint green.
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn().mockResolvedValue(makeNormalized()),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi.fn().mockResolvedValue(
        makeRunIndex({
          runs: [
            makeRunSummary({
              run_id: "ui_run_legacy_no_note",
              exit_code: 0,
              exit_clean: true,
              is_clean: null,
            }),
          ],
        }),
      ),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();

    await component.loadRun("ui_run_legacy_no_note");
    fixture.detectChanges();

    expect(component.response()?.is_clean).toBe(false);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).not.toContain("Clean run");
  });

  it("surfaces the listRuns failure reason in the sidebar (reviewer: no silent catch)", async () => {
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn(),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(503, "launcher_unreachable", "down")),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Couldn't load runs");
    expect(text).toContain("launcher_unreachable");
  });

  it("loadRun surfaces a 404 via the typed error envelope", async () => {
    serviceMock.getNormalized.mockRejectedValue(
      new LeanSidecarApiError(404, "normalized_missing", "not present"),
    );

    await component.loadRun("ui_run_missing");
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run failed");
    expect(text).toContain("normalized_missing");
  });

  it("does not crash when listRuns rejects on init", async () => {
    TestBed.resetTestingModule();
    serviceMock = {
      startTrustedRun: vi.fn(),
      getNormalized: vi.fn(),
      getManifest: vi.fn().mockRejectedValue(new LeanSidecarApiError(404, "manifest_missing", "n/a")),
      getLogTail: vi.fn(),
      getObservationsCsv: vi.fn(),
      reconcileRun: vi
        .fn()
        .mockRejectedValue(new LeanSidecarApiError(404, "normalized_missing", "n/a")),
      listRuns: vi.fn().mockRejectedValue(new Error("network down")),
    };
    await TestBed.configureTestingModule({
      imports: [LeanLabComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: LeanSidecarService, useValue: serviceMock },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(LeanLabComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    // Sidebar shows the empty state, page itself rendered fine.
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("LEAN Sidecar Lab");
    expect(text).toContain("No runs yet");
    expect(component.runs()).toEqual([]);
  });

  it("Phase 4e: loadRun rehydrates symbol + window + cash from the manifest", async () => {
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.getManifest.mockResolvedValue({
      run_id: "ui_run_rehydrate",
      parameters: { symbol: "MSFT", starting_cash: "250000" },
      requested_window_ms: { start_ms: 1_736_121_600_000, end_ms: 1_736_467_200_000 },
    });

    const initialRunId = component.form.controls.runId.value;
    await component.loadRun("ui_run_rehydrate");
    fixture.detectChanges();

    expect(component.form.controls.symbol.value).toBe("MSFT");
    expect(component.form.controls.startingCash.value).toBe(250000);
    expect(component.form.controls.startDate.value).toBe("2025-01-06");
    expect(component.form.controls.endDate.value).toBe("2025-01-10");
    // Toggle resets to off (manifest doesn't store the source itself).
    expect(component.form.controls.useCustomAlgorithm.value).toBe(false);
    // Fresh runId so re-running the form lands in a new workspace.
    expect(component.form.controls.runId.value).not.toBe(initialRunId);
    expect(component.form.controls.runId.value).not.toBe("ui_run_rehydrate");
  });

  it("Phase 4e: starting_cash as number (not string) also rehydrates", async () => {
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.getManifest.mockResolvedValue({
      parameters: { symbol: "SPY", starting_cash: 500000 },
      requested_window_ms: { start_ms: 1_736_121_600_000, end_ms: 1_736_467_200_000 },
    });

    await component.loadRun("ui_run_numeric_cash");

    expect(component.form.controls.startingCash.value).toBe(500000);
  });

  it("Phase 4e: manifest 404 leaves form at its current values (result panel still renders)", async () => {
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.getManifest.mockRejectedValue(
      new LeanSidecarApiError(404, "manifest_missing", "legacy run"),
    );
    component.form.patchValue({
      symbol: "AAPL",
      startingCash: 99_000,
      startDate: "2026-01-01",
      endDate: "2026-01-05",
    });

    await component.loadRun("ui_run_legacy");
    fixture.detectChanges();

    // Form unchanged — manifest fetch failure must not clobber it.
    expect(component.form.controls.symbol.value).toBe("AAPL");
    expect(component.form.controls.startingCash.value).toBe(99_000);
    expect(component.form.controls.startDate.value).toBe("2026-01-01");
    expect(component.form.controls.endDate.value).toBe("2026-01-05");
    // Result panel still rendered.
    expect(component.response()?.run_id).toBe("ui_run_legacy");
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Equity snapshot");
  });

  it("Phase 4e: rejects nonsensical starting_cash (under min) instead of patching it", async () => {
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.getManifest.mockResolvedValue({
      parameters: { symbol: "SPY", starting_cash: "5" },
      requested_window_ms: { start_ms: 1_736_121_600_000, end_ms: 1_736_467_200_000 },
    });
    const initialCash = component.form.controls.startingCash.value;

    await component.loadRun("ui_run_tiny_cash");

    // Below-min cash from manifest must not be patched in — would
    // immediately invalidate the form. Symbol + dates still rehydrate.
    expect(component.form.controls.startingCash.value).toBe(initialCash);
    expect(component.form.controls.symbol.value).toBe("SPY");
  });

  it("Phase 5a: 'Reconcile fees' button fetches the report and renders the panel", async () => {
    // Submit a clean run first so the response panel is visible (the
    // reconcile button only renders inside that panel).
    serviceMock.startTrustedRun.mockResolvedValue(makeResponse({ run_id: "ui_run_recon" }));
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.reconcileRun.mockResolvedValue({
      run_id: "ui_run_recon",
      algorithm_id: "MyAlgorithm",
      normalized_parser_version: "phase-3a-r1",
      total_fill_events: 2,
      matched_count: 1,
      divergent_count: 1,
      commission_atol: "0.01",
      total_recorded_fees: "6.00",
      total_expected_ibkr_fees: "2.00",
      divergences: [
        {
          order_event_id: 2,
          order_id: 200,
          symbol: "SPY",
          ms_utc: 1_736_121_600_000,
          fill_quantity: 100,
          fill_price: "580.50",
          recorded_fee: "5.00",
          expected_ibkr_fee: "1.00",
          delta: "4.00",
          category: "commission_drift",
        },
      ],
    });

    await component.submit();
    fixture.detectChanges();
    await fixture.whenStable();

    // Click the reconcile button via the component method (component-
    // level test — the parent handler is the user-observable surface).
    await component.reconcileFees();
    fixture.detectChanges();

    expect(serviceMock.reconcileRun).toHaveBeenCalledWith("ui_run_recon");
    expect(component.reconciliation()?.divergent_count).toBe(1);

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    // Categorized counts visible on the panel.
    expect(text).toContain("Total fills");
    expect(text).toContain("Matched");
    expect(text).toContain("Divergent");
    // Totals visible.
    expect(text).toContain("6.00");
    expect(text).toContain("2.00");
    // Divergence row visible with the category label.
    expect(text).toContain("commission_drift");
  });

  it("Phase 5a P2: stale reconcile response is dropped when the active run changed mid-flight", async () => {
    // Arrange: a slow reconcile resolves to run A; before it resolves
    // the user submits run B. Expect: B's panel stays empty (no stale
    // paint from A) and ``reconciling`` is cleared.
    let resolveReconcile: (report: RunReconciliationReport) => void = () => {
      throw new Error("reconcileRun mock was not invoked before navigation");
    };
    const reportForA: RunReconciliationReport = {
      run_id: "ui_run_a",
      algorithm_id: "MyAlgorithm",
      normalized_parser_version: "phase-3a-r1",
      total_fill_events: 1,
      matched_count: 0,
      divergent_count: 1,
      commission_atol: "0.01",
      total_recorded_fees: "5.00",
      total_expected_ibkr_fees: "1.00",
      divergences: [],
    };

    serviceMock.startTrustedRun.mockResolvedValueOnce(makeResponse({ run_id: "ui_run_a" }));
    serviceMock.getNormalized.mockResolvedValue(makeNormalized());
    serviceMock.reconcileRun.mockImplementationOnce(
      () =>
        new Promise<RunReconciliationReport>((resolve) => {
          resolveReconcile = resolve;
        }),
    );

    await component.submit();
    fixture.detectChanges();

    // Fire and forget — we'll resolve it after navigating away.
    const inFlight = component.reconcileFees();

    // Simulate the user starting a new run B (this clears
    // ``reconciliation`` + ``reconcileError`` and replaces ``response``).
    serviceMock.startTrustedRun.mockResolvedValueOnce(makeResponse({ run_id: "ui_run_b" }));
    await component.submit();
    fixture.detectChanges();

    expect(component.response()?.run_id).toBe("ui_run_b");
    expect(component.reconciliation()).toBeNull();

    // Now the original POST resolves with run A's report.
    resolveReconcile(reportForA);
    await inFlight;
    fixture.detectChanges();

    // Race fix: the stale report must NOT paint onto run B's panel.
    expect(component.reconciliation()).toBeNull();
    expect(component.reconcileError()).toBeNull();
    expect(component.reconciling()).toBe(false);
  });

  it("renders the launcher's typed rejection envelope on a 400", async () => {
    serviceMock.startTrustedRun.mockRejectedValue(
      new LeanSidecarApiError(400, "workspace_not_staged", "stage first"),
    );

    await component.submit();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("Run failed");
    expect(text).toContain("workspace_not_staged");
    expect(text).toContain("stage first");
  });
});
