import { provideZonelessChangeDetection, signal } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EngineLabRunHistoryComponent } from "./engine-lab-run-history.component";
import {
  BACKTEST_RUNS_QUERY,
  BacktestRunNode,
  UPDATE_BACKTEST_RUN_NOTES_MUTATION,
} from "../../../graphql/backtest-runs.query";
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

function baseNode(over: Partial<BacktestRunNode> = {}): BacktestRunNode {
  return {
    id: 30,
    source: "engine",
    engine: "PYTHON",
    strategyName: "sma_crossover",
    leanRunId: null,
    parameters: '{"symbol":"AAPL","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-10",
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
    trades: [{ isSyntheticExit: false }],
    ...over,
  };
}

const FAKE_NODES: BacktestRunNode[] = [
  baseNode({ id: 30, strategyName: "sma_crossover" }),
  baseNode({
    id: 31,
    strategyName: "rsi_mean_reversion",
    engine: "LEAN",
    source: "lean-sidecar",
    endDate: "2025-01-06",
    executedAt: Date.UTC(2026, 4, 19, 8, 5),
    totalTrades: 1,
    totalPnL: -5.0,
    trades: [{ isSyntheticExit: true }],
  }),
];

function makeApollo(nodes: BacktestRunNode[] = FAKE_NODES) {
  const valueChanges$ = of({
    data: {
      backtestRuns: {
        pageInfo: { hasNextPage: false, endCursor: null },
        nodes,
      },
    },
  });
  const refetch = vi.fn();
  const mutate = vi.fn().mockReturnValue(of({ data: { updateBacktestRunNotes: { id: 30, notes: "new" } } }));
  return {
    watchQuery: vi.fn().mockReturnValue({ valueChanges: valueChanges$, refetch }),
    mutate,
    _refetch: refetch,
    _mutate: mutate,
  };
}

async function setup(
  apolloStub = makeApollo(),
  navigateSpy = vi.fn(),
): Promise<ComponentFixture<EngineLabRunHistoryComponent>> {
  await TestBed.configureTestingModule({
    imports: [EngineLabRunHistoryComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: apolloStub },
      { provide: Router, useValue: { navigate: navigateSpy } },
      { provide: JobsService, useValue: jobsServiceMock },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(EngineLabRunHistoryComponent);
  fixture.detectChanges();
  return fixture;
}

describe("EngineLabRunHistoryComponent", () => {
  beforeEach(() => {
    installLocalStorage();
    localStorage.clear();
    fakeJobsSignal.set([]);
  });

  it("queries backtestRuns with engine=null by default (All filter)", async () => {
    const apollo = makeApollo();
    await setup(apollo);
    expect(apollo.watchQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        query: BACKTEST_RUNS_QUERY,
        variables: expect.objectContaining({ engine: null }),
      }),
    );
  });

  it("renders strategy names from mapped rows", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("sma_crossover");
    expect(html).toContain("rsi_mean_reversion");
  });

  it("renders symbol extracted from parameters JSON", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("AAPL");
  });

  it("renders Engine column badges (Engine Lab / LEAN)", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Engine Lab");
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
    const fixture = await setup(makeApollo([]));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("No runs yet");
  });

  it("extracts null symbol when parameters is null", async () => {
    const nodes: BacktestRunNode[] = [
      baseNode({
        id: 40,
        strategyName: "no_params",
        parameters: null,
        endDate: "2025-01-10",
        executedAt: Date.UTC(2026, 4, 19, 9, 0),
        totalTrades: 0,
        totalPnL: 0,
        trades: [],
      }),
    ];
    const fixture = await setup(makeApollo(nodes));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("—");
  });
});

describe("EngineLabRunHistoryComponent — filter dropdown (PR B.3)", () => {
  it("changing the engine filter to PYTHON refetches with engine=PYTHON", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);

    const select = fixture.nativeElement.querySelector(
      '[data-testid="engine-filter"]',
    ) as HTMLSelectElement;
    select.value = "PYTHON";
    select.dispatchEvent(new Event("change"));
    fixture.detectChanges();

    expect(apollo._refetch).toHaveBeenCalledWith(
      expect.objectContaining({ engine: "PYTHON" }),
    );
  });

  it("changing the engine filter to LEAN refetches with engine=LEAN", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);

    const select = fixture.nativeElement.querySelector(
      '[data-testid="engine-filter"]',
    ) as HTMLSelectElement;
    select.value = "LEAN";
    select.dispatchEvent(new Event("change"));
    fixture.detectChanges();

    expect(apollo._refetch).toHaveBeenCalledWith(
      expect.objectContaining({ engine: "LEAN" }),
    );
  });
});

describe("EngineLabRunHistoryComponent — notes editing (PR B.3)", () => {
  it("notesEdited from the inner table triggers the GraphQL mutation", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);

    await fixture.componentInstance.onNotesEdited({ id: "30", notes: "good run" });

    expect(apollo._mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        mutation: UPDATE_BACKTEST_RUN_NOTES_MUTATION,
        variables: { id: 30, notes: "good run" },
      }),
    );
  });
});

describe("EngineLabRunHistoryComponent — CSV export (PR B.3)", () => {
  it("export button is disabled when there are no rows", async () => {
    const fixture = await setup(makeApollo([]));
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

  it("refetches when an engine_backtest job transitions to completed", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    // The filter effect fires one refetch on init; clear so we can assert
    // exactly what the job-completed effect produces.
    apollo._refetch.mockClear();

    fakeJobsSignal.set([makeJob({ id: "py-1", type: "engine_backtest", status: "completed" })]);
    fixture.detectChanges();

    expect(apollo._refetch).toHaveBeenCalledTimes(1);
  });

  it("refetches when a lean_engine_run job transitions to completed", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    apollo._refetch.mockClear();

    fakeJobsSignal.set([makeJob({ id: "lean-1", type: "lean_engine_run", status: "completed" })]);
    fixture.detectChanges();

    expect(apollo._refetch).toHaveBeenCalledTimes(1);
  });

  it("does not refetch when a non-engine job (dataset-zip) completes", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    apollo._refetch.mockClear();

    fakeJobsSignal.set([makeJob({ id: "ds-1", type: "dataset-zip", status: "completed" })]);
    fixture.detectChanges();

    expect(apollo._refetch).not.toHaveBeenCalled();
  });

  it("does not refetch for running/failed/cancelled engine jobs", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    apollo._refetch.mockClear();

    fakeJobsSignal.set([
      makeJob({ id: "r-1", type: "engine_backtest", status: "running" }),
      makeJob({ id: "f-1", type: "engine_backtest", status: "failed" }),
      makeJob({ id: "c-1", type: "engine_backtest", status: "cancelled" }),
    ]);
    fixture.detectChanges();

    expect(apollo._refetch).not.toHaveBeenCalled();
  });

  it("does not re-refetch on subsequent signal ticks for the same completed job", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    apollo._refetch.mockClear();

    // Initial completion → one refetch.
    fakeJobsSignal.set([makeJob({ id: "py-2", type: "engine_backtest", status: "completed" })]);
    fixture.detectChanges();
    expect(apollo._refetch).toHaveBeenCalledTimes(1);

    // Same job reported again with a fresh recentLogs entry → should not
    // refire the refetch. (Real-world trigger: recentLogs rolls after
    // completion as the worker drains its log queue.)
    fakeJobsSignal.set([
      makeJob({
        id: "py-2",
        type: "engine_backtest",
        status: "completed",
        recentLogs: [{ level: "info", message: "drained", ts: 1, seq: 1 }],
        logSeq: 1,
      }),
    ]);
    fixture.detectChanges();
    expect(apollo._refetch).toHaveBeenCalledTimes(1);
  });

  it("refetches separately for two distinct engine jobs that complete in sequence", async () => {
    const apollo = makeApollo();
    const fixture = await setup(apollo);
    apollo._refetch.mockClear();

    fakeJobsSignal.set([makeJob({ id: "first", type: "engine_backtest", status: "completed" })]);
    fixture.detectChanges();
    expect(apollo._refetch).toHaveBeenCalledTimes(1);

    fakeJobsSignal.set([
      makeJob({ id: "first", type: "engine_backtest", status: "completed" }),
      makeJob({ id: "second", type: "lean_engine_run", status: "completed" }),
    ]);
    fixture.detectChanges();
    expect(apollo._refetch).toHaveBeenCalledTimes(2);
  });
});

describe("EngineLabRunHistoryComponent — column visibility (PR B.3)", () => {
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
