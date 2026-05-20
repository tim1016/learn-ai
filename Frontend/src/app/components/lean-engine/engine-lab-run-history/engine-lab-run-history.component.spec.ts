import { provideZonelessChangeDetection } from "@angular/core";
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

function baseNode(over: Partial<BacktestRunNode> = {}): BacktestRunNode {
  return {
    id: "30",
    source: "engine",
    engine: "PYTHON",
    strategyName: "sma_crossover",
    leanRunId: null,
    parameters: '{"symbol":"AAPL","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: "2026-05-19T08:00:00Z",
    totalTrades: 3,
    totalPnL: 42.0,
    commissionPerOrder: 1.0,
    brokeragePolicy: "algorithm_default",
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
  baseNode({ id: "30", strategyName: "sma_crossover" }),
  baseNode({
    id: "31",
    strategyName: "rsi_mean_reversion",
    engine: "LEAN",
    source: "lean-sidecar",
    endDate: "2025-01-06",
    executedAt: "2026-05-19T08:05:00Z",
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
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(EngineLabRunHistoryComponent);
  fixture.detectChanges();
  return fixture;
}

describe("EngineLabRunHistoryComponent", () => {
  beforeEach(() => {
    localStorage.clear();
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

  it("renders the DataPolicy bars summary (m/1 → m/15)", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("m/1 → m/15");
  });

  it("renders 'Open at end' for rows with a synthetic exit", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Open at end");
  });

  it("navigates to /runs/compare when onCompare is called", async () => {
    const navigateSpy = vi.fn().mockResolvedValue(true);
    const fixture = await setup(makeApollo(), navigateSpy);
    fixture.componentInstance.onCompare({ leftId: "30", rightId: "31" });
    expect(navigateSpy).toHaveBeenCalledWith(
      ["/runs/compare"],
      { queryParams: { left: "30", right: "31" } },
    );
  });

  it("renders the empty state when no rows are returned", async () => {
    const fixture = await setup(makeApollo([]));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("No runs yet");
  });

  it("extracts null symbol when parameters is null", async () => {
    const nodes: BacktestRunNode[] = [
      baseNode({
        id: "40",
        strategyName: "no_params",
        parameters: null,
        endDate: "2025-01-10",
        executedAt: "2026-05-19T09:00:00Z",
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
      expect(captured).not.toBeNull();
      // Read the blob via FileReader so the test environment doesn't depend
      // on Blob.prototype.text() (jsdom support varies).
      const text = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsText(captured!);
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

describe("EngineLabRunHistoryComponent — column visibility (PR B.3)", () => {
  it("toggle persists the choice to localStorage", async () => {
    const fixture = await setup();

    fixture.componentInstance.toggleColumn("notes");
    fixture.detectChanges();

    const raw = localStorage.getItem("engine-lab-history.columns.v1");
    expect(raw).not.toBeNull();
    const ids = JSON.parse(raw!) as string[];
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
