import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";
import { LeanLabRunHistoryComponent } from "./lean-lab-run-history.component";
import { BacktestRunNode, BACKTEST_RUNS_QUERY } from "../../../graphql/backtest-runs.query";

function baseNode(over: Partial<BacktestRunNode> = {}): BacktestRunNode {
  return {
    id: "10",
    source: "lean-sidecar",
    engine: "LEAN",
    strategyName: "ema_crossover",
    leanRunId: "ui_run_abc",
    parameters: '{"symbol":"SPY","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: "2026-05-19T02:49:00Z",
    totalTrades: 1,
    totalPnL: 9.0,
    commissionPerOrder: 0,
    brokeragePolicy: null,
    notes: null,
    dataPolicy: null,
    trades: [{ isSyntheticExit: false }],
    ...over,
  };
}

const FAKE_NODES: BacktestRunNode[] = [
  baseNode({ id: "10", strategyName: "ema_crossover", leanRunId: "ui_run_abc" }),
  baseNode({
    id: "11",
    strategyName: "trusted_default",
    leanRunId: "ui_run_xyz",
    endDate: "2025-01-06",
    executedAt: "2026-05-19T02:50:00Z",
    totalPnL: 5.0,
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
  return { watchQuery: vi.fn().mockReturnValue({ valueChanges: valueChanges$ }) };
}

async function setup(
  apolloStub = makeApollo(),
  navigateSpy = vi.fn(),
): Promise<ComponentFixture<LeanLabRunHistoryComponent>> {
  await TestBed.configureTestingModule({
    imports: [LeanLabRunHistoryComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: apolloStub },
      { provide: Router, useValue: { navigate: navigateSpy } },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(LeanLabRunHistoryComponent);
  fixture.detectChanges();
  return fixture;
}

describe("LeanLabRunHistoryComponent", () => {
  it("queries backtestRuns with engine=LEAN", async () => {
    const apollo = makeApollo();
    await setup(apollo);
    expect(apollo.watchQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        query: BACKTEST_RUNS_QUERY,
        variables: expect.objectContaining({ engine: "LEAN" }),
      }),
    );
  });

  it("renders strategy names from mapped rows", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("ema_crossover");
    expect(html).toContain("trusted_default");
  });

  it("renders symbol extracted from parameters JSON", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("SPY");
  });

  it("renders the LEAN engine badge", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("LEAN");
  });

  it("renders 'Open at end' for rows with a synthetic exit", async () => {
    const fixture = await setup();
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(html).toContain("Open at end");
  });

  it("navigates to /runs/compare when onCompare is called", async () => {
    const navigateSpy = vi.fn().mockResolvedValue(true);
    const fixture = await setup(makeApollo(), navigateSpy);
    fixture.componentInstance.onCompare({ leftId: "10", rightId: "11" });
    expect(navigateSpy).toHaveBeenCalledWith(
      ["/runs/compare"],
      { queryParams: { left: "10", right: "11" } },
    );
  });

  it("emits runSelected with the leanRunId when a row is clicked", async () => {
    const emitted: string[] = [];
    const fixture = await setup();
    fixture.componentInstance.runSelected.subscribe((id) => emitted.push(id));

    const rows = fixture.nativeElement.querySelectorAll("tbody tr") as NodeListOf<HTMLTableRowElement>;
    rows[0].click(); // row id="10", leanRunId="ui_run_abc"

    expect(emitted).toEqual(["ui_run_abc"]);
  });

  it("does not emit runSelected when the row has no leanRunId", async () => {
    const nodes = [
      baseNode({
        id: "20",
        strategyName: "no_lean_id",
        leanRunId: null,
        parameters: null,
        totalTrades: 0,
        totalPnL: 0,
        trades: [],
      }),
    ];
    const emitted: string[] = [];
    const fixture = await setup(makeApollo(nodes));
    fixture.componentInstance.runSelected.subscribe((id) => emitted.push(id));

    const rows = fixture.nativeElement.querySelectorAll("tbody tr") as NodeListOf<HTMLTableRowElement>;
    rows[0].click();

    expect(emitted).toHaveLength(0);
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
      baseNode({
        id: "20",
        strategyName: "no_params",
        leanRunId: null,
        parameters: null,
        totalTrades: 0,
        totalPnL: 0,
        trades: [],
      }),
    ];
    const fixture = await setup(makeApollo(nodes));
    const html = (fixture.nativeElement as HTMLElement).textContent ?? "";
    // null symbol renders as '—' in the shared component
    expect(html).toContain("—");
  });
});
