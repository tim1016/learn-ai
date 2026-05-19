import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";
import { EngineLabRunHistoryComponent } from "./engine-lab-run-history.component";
import { BacktestRunNode, BACKTEST_RUNS_QUERY } from "../../../graphql/backtest-runs.query";

const FAKE_NODES: BacktestRunNode[] = [
  {
    id: "30",
    source: "engine",
    strategyName: "sma_crossover",
    leanRunId: null,
    parameters: '{"symbol":"AAPL","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: "2026-05-19T08:00:00Z",
    totalTrades: 3,
    totalPnL: 42.0,
    trades: [{ isSyntheticExit: false }],
  },
  {
    id: "31",
    source: "engine",
    strategyName: "rsi_mean_reversion",
    leanRunId: null,
    parameters: '{"symbol":"AAPL","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-06",
    executedAt: "2026-05-19T08:05:00Z",
    totalTrades: 1,
    totalPnL: -5.0,
    trades: [{ isSyntheticExit: true }],
  },
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
  return { watchQuery: vi.fn().mockReturnValue({ valueChanges: valueChanges$ }) };
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
  it("queries backtestRuns with engine=ENGINE", async () => {
    const apollo = makeApollo();
    await setup(apollo);
    expect(apollo.watchQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        query: BACKTEST_RUNS_QUERY,
        variables: expect.objectContaining({ engine: "ENGINE" }),
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

  it("renders the Engine Lab badge", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Engine Lab");
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
    // extractSymbol is a module-private function — test indirectly by
    // verifying the component renders a dash for a null-parameters row.
    const nodes: BacktestRunNode[] = [
      {
        id: "40",
        source: "engine",
        strategyName: "no_params",
        leanRunId: null,
        parameters: null,
        startDate: "2025-01-06",
        endDate: "2025-01-10",
        executedAt: "2026-05-19T09:00:00Z",
        totalTrades: 0,
        totalPnL: 0,
        trades: [],
      },
    ];
    const fixture = await setup(makeApollo(nodes));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    // null symbol renders as '—' in the shared component
    expect(html).toContain("—");
  });
});
