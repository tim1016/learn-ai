import { provideZonelessChangeDetection, signal } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Router } from "@angular/router";
import { of } from "rxjs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EngineLabRunHistoryComponent } from "./engine-lab-run-history.component";
import { BacktestRunsService, HISTORY_PAGE_SIZE } from "../../../services/backtest-runs.service";
import type { BacktestRunSummary } from "../../../services/backtest-runs.types";
import { JobsService, JobState } from "../../../services/jobs.service";

/** Writable signal the auto-refresh tests use to drive JobsService state.
 *  Reset in the outer `beforeEach` so cross-test pollution doesn't trip
 *  the seen-ids dedupe set. */
const fakeJobsSignal = signal<JobState[]>([]);
const jobsServiceMock = { jobs: () => fakeJobsSignal() };

function installLocalStorage(): void {
  const store = new Map<string, string>();
  const storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    removeItem: (key: string) => store.delete(key),
    setItem: (key: string, value: string) => store.set(key, String(value)),
  } as Storage;
  vi.stubGlobal("localStorage", storage);
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

beforeEach(() => {
  installLocalStorage();
});

function makeJob(over: Partial<JobState>): JobState {
  return {
    id: "job-1",
    type: "engine_backtest",
    status: "running",
    recentLogs: [],
    logSeq: 0,
    ...over,
  };
}

function baseRun(over: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    id: 30,
    source: "engine",
    engine: "PYTHON",
    strategyName: "sma_crossover",
    symbol: "AAPL",
    leanRunId: null,
    parameters: '{"symbol":"AAPL","starting_cash":100000}',
    startDate: 1736139600000, // 2025-01-06 ET midnight
    endDate: 1736485200000, // 2025-01-10 ET midnight
    executedAt: Date.UTC(2026, 4, 19, 8, 0),
    totalTrades: 3,
    totalPnL: 42.0,
    commissionPerOrder: 1.0,
    brokeragePolicy: "algorithm_default",
    verdictGrade: null,
    verdictSignal: null,
    parityGroupId: null,
    notes: null,
    dataPolicy: {
      source: "polygon",
      symbol: "AAPL",
      adjusted: true,
      session: "regular",
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
      timestamp_policy: "bar_close_ms_utc",
      timezone: "America/New_York",
      provider_kind: "live",
      fixture_id: null,
      fixture_sha256: null,
    },
    hasSyntheticExit: false,
    ...over,
  };
}

const FAKE_RUNS: BacktestRunSummary[] = [
  baseRun({ id: 30, strategyName: "sma_crossover" }),
  baseRun({
    id: 31,
    strategyName: "rsi_mean_reversion",
    engine: "LEAN",
    source: "lean-sidecar",
    endDate: 1736139600000,
    executedAt: Date.UTC(2026, 4, 19, 8, 5),
    totalTrades: 1,
    totalPnL: -5.0,
    hasSyntheticExit: true,
  }),
];

function makeRunsService(runs: BacktestRunSummary[] = FAKE_RUNS) {
  return {
    list: vi.fn(() => of(runs)),
    get: vi.fn(),
    updateNotes: vi.fn((id: number, notes: string) => of({ id, notes })),
  };
}

async function setup(
  runsService = makeRunsService(),
  navigateSpy = vi.fn(),
): Promise<ComponentFixture<EngineLabRunHistoryComponent>> {
  await TestBed.configureTestingModule({
    imports: [EngineLabRunHistoryComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: BacktestRunsService, useValue: runsService },
      { provide: Router, useValue: { navigate: navigateSpy } },
      { provide: JobsService, useValue: jobsServiceMock },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(EngineLabRunHistoryComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

describe("EngineLabRunHistoryComponent", () => {
  beforeEach(() => {
    installLocalStorage();
    localStorage.clear();
    fakeJobsSignal.set([]);
  });

  it("lists every engine's runs by default (All filter) as one fixed page", async () => {
    const runs = makeRunsService();
    await setup(runs);
    expect(runs.list).toHaveBeenCalledWith(null, HISTORY_PAGE_SIZE);
  });

  it("renders strategy names from mapped rows", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("sma_crossover");
    expect(html).toContain("rsi_mean_reversion");
  });

  it("renders the symbol stored on the run", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("AAPL");
  });

  it("renders Engine column badges (Python / LEAN)", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Python");
    expect(html).toContain("LEAN");
  });

  it("renders the DataPolicy bars summary with explicit input and strategy cadences", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Input M1 / Strategy M15");
  });

  it("renders 'Open at end' for rows with a synthetic exit", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Open at end");
  });

  it("renders the empty state when no rows are returned", async () => {
    const fixture = await setup(makeRunsService([]));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("No runs yet");
  });
});

describe("EngineLabRunHistoryComponent — filter dropdown", () => {
  it("changing the engine filter to PYTHON re-reads with engine=PYTHON", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);

    const select = fixture.nativeElement.querySelector(
      '[data-testid="engine-filter"]',
    ) as HTMLSelectElement;
    select.value = "PYTHON";
    select.dispatchEvent(new Event("change"));
    fixture.detectChanges();
    await fixture.whenStable();

    expect(runs.list).toHaveBeenLastCalledWith("PYTHON", HISTORY_PAGE_SIZE);
  });

  it("changing the engine filter to LEAN re-reads with engine=LEAN", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);

    const select = fixture.nativeElement.querySelector(
      '[data-testid="engine-filter"]',
    ) as HTMLSelectElement;
    select.value = "LEAN";
    select.dispatchEvent(new Event("change"));
    fixture.detectChanges();
    await fixture.whenStable();

    expect(runs.list).toHaveBeenLastCalledWith("LEAN", HISTORY_PAGE_SIZE);
  });
});

describe("EngineLabRunHistoryComponent — notes editing", () => {
  it("notesEdited from the inner table persists through the run service and patches the row", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);

    await fixture.componentInstance.onNotesEdited({ id: "30", notes: "good run" });
    fixture.detectChanges();

    expect(runs.updateNotes).toHaveBeenCalledWith(30, "good run");
    expect(fixture.componentInstance.rows().find((row) => row.id === "30")?.notes).toBe("good run");
    // The loaded page is patched in place; no re-read of the list.
    expect(runs.list).toHaveBeenCalledTimes(1);
  });
});

describe("EngineLabRunHistoryComponent — CSV export", () => {
  it("export button is disabled when there are no rows", async () => {
    const fixture = await setup(makeRunsService([]));
    const button = fixture.nativeElement.querySelector(
      '[data-testid="export-csv"]',
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("export button is enabled when rows exist", async () => {
    const fixture = await setup();
    const button = fixture.nativeElement.querySelector(
      '[data-testid="export-csv"]',
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("clicking Export downloads a CSV with the rendered rows", async () => {
    const fixture = await setup();
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    let captured: Blob | null = null;
    URL.createObjectURL = vi.fn((blob: Blob) => {
      captured = blob;
      return "blob://stub";
    });
    URL.revokeObjectURL = vi.fn();

    try {
      const button = fixture.nativeElement.querySelector(
        '[data-testid="export-csv"]',
      ) as HTMLButtonElement;
      button.click();
      const blob = captured;
      expect(blob).not.toBeNull();
      if (!blob) return;
      // Read the blob via FileReader so the test environment doesn't depend
      // on Blob.prototype.text() (jsdom support varies).
      const text = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsText(blob);
      });
      expect(text).toContain("sma_crossover");
      expect(text).toContain("AAPL");
      expect(text).toContain("minute/1");
      expect(text).toContain("minute/15");
    } finally {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
    }
  });
});

describe("EngineLabRunHistoryComponent — auto-refresh on job.completed (#468)", () => {
  beforeEach(() => {
    installLocalStorage();
    localStorage.clear();
    fakeJobsSignal.set([]);
  });

  async function settle(fixture: ComponentFixture<EngineLabRunHistoryComponent>): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  it("re-reads when an engine_backtest job transitions to completed, keeping the rows on screen meanwhile", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    fakeJobsSignal.set([makeJob({ id: "py-1", type: "engine_backtest", status: "completed" })]);
    fixture.detectChanges();
    // A reload keeps the previous page visible while the new one is in flight.
    expect(fixture.componentInstance.rows().length).toBe(2);
    await settle(fixture);

    expect(runs.list).toHaveBeenCalledTimes(1);
  });

  it("re-reads when a lean_engine_run job transitions to completed", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    fakeJobsSignal.set([makeJob({ id: "lean-1", type: "lean_engine_run", status: "completed" })]);
    await settle(fixture);

    expect(runs.list).toHaveBeenCalledTimes(1);
  });

  it("does not re-read when a non-engine job (dataset-zip) completes", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    fakeJobsSignal.set([makeJob({ id: "ds-1", type: "dataset-zip", status: "completed" })]);
    await settle(fixture);

    expect(runs.list).not.toHaveBeenCalled();
  });

  it("does not re-read for running/failed/cancelled engine jobs", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    fakeJobsSignal.set([
      makeJob({ id: "r-1", type: "engine_backtest", status: "running" }),
      makeJob({ id: "f-1", type: "engine_backtest", status: "failed" }),
      makeJob({ id: "c-1", type: "engine_backtest", status: "cancelled" }),
    ]);
    await settle(fixture);

    expect(runs.list).not.toHaveBeenCalled();
  });

  it("does not re-read on subsequent signal ticks for the same completed job", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    // Initial completion → one re-read.
    fakeJobsSignal.set([makeJob({ id: "py-2", type: "engine_backtest", status: "completed" })]);
    await settle(fixture);
    expect(runs.list).toHaveBeenCalledTimes(1);

    // Same job reported again with a fresh recentLogs entry → should not
    // refire. (Real-world trigger: recentLogs rolls after completion as the
    // worker drains its log queue.)
    fakeJobsSignal.set([
      makeJob({
        id: "py-2",
        type: "engine_backtest",
        status: "completed",
        recentLogs: [{ level: "info", message: "drained", ts: 1, seq: 1 }],
        logSeq: 1,
      }),
    ]);
    await settle(fixture);
    expect(runs.list).toHaveBeenCalledTimes(1);
  });

  it("re-reads separately for two distinct engine jobs that complete in sequence", async () => {
    const runs = makeRunsService();
    const fixture = await setup(runs);
    runs.list.mockClear();

    fakeJobsSignal.set([makeJob({ id: "first", type: "engine_backtest", status: "completed" })]);
    await settle(fixture);
    expect(runs.list).toHaveBeenCalledTimes(1);

    fakeJobsSignal.set([
      makeJob({ id: "first", type: "engine_backtest", status: "completed" }),
      makeJob({ id: "second", type: "lean_engine_run", status: "completed" }),
    ]);
    await settle(fixture);
    expect(runs.list).toHaveBeenCalledTimes(2);
  });
});

describe("EngineLabRunHistoryComponent — column visibility", () => {
  it("toggle persists the choice to localStorage", async () => {
    const fixture = await setup();

    fixture.componentInstance.toggleColumn("notes");
    fixture.detectChanges();

    const raw = localStorage.getItem("engine-lab-history.columns.v1");
    expect(raw).not.toBeNull();
    if (!raw) return;
    const ids = JSON.parse(raw) as string[];
    expect(ids).not.toContain("notes");
  });

  it("resetColumns restores the default visible set", async () => {
    const fixture = await setup();

    fixture.componentInstance.toggleColumn("notes");
    fixture.componentInstance.toggleColumn("bars");
    fixture.componentInstance.resetColumns();
    fixture.detectChanges();

    expect(fixture.componentInstance.isColumnVisible("notes")).toBe(true);
    expect(fixture.componentInstance.isColumnVisible("bars")).toBe(true);
  });
});
