import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed, type ComponentFixture } from "@angular/core/testing";
import { ActivatedRoute } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";
import type { RunComparisonResult } from "../../graphql/compare-backtest-runs.query";
import { RunComparisonComponent } from "./run-comparison.component";

function buildResult(overrides: Partial<RunComparisonResult> = {}): RunComparisonResult {
  return {
    left: {
      id: 1,
      source: "lean-sidecar",
      strategyName: "ema_crossover",
      leanRunId: "ui_run_left",
      totalTrades: 1,
      totalPnL: 10,
      finalEquity: 100010,
      trades: [],
    },
    right: {
      id: 2,
      source: "engine",
      strategyName: "ema_crossover",
      leanRunId: null,
      totalTrades: 1,
      totalPnL: 12,
      finalEquity: 100012,
      trades: [],
    },
    guardrails: {
      sameAlgorithm: true,
      sameSymbol: true,
      sameWindow: true,
      sameParameters: true,
      warnings: [],
    },
    summary: {
      pnlDelta: 2,
      tradeCountDelta: 0,
      winRateDelta: 0,
      feesDelta: 0,
      finalEquityDelta: 2,
    },
    divergences: [],
    firstDivergenceMsUtc: null,
    ...overrides,
  };
}

function makeApollo(result: RunComparisonResult | null) {
  const valueChanges$ = of({ data: { compareBacktestRuns: result } });
  return { watchQuery: vi.fn().mockReturnValue({ valueChanges: valueChanges$ }) };
}

async function setup(
  result: RunComparisonResult | null = buildResult(),
  queryParams: Record<string, string> = { left: "1", right: "2" },
): Promise<ComponentFixture<RunComparisonComponent>> {
  await TestBed.configureTestingModule({
    imports: [RunComparisonComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: makeApollo(result) },
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap: of({
            get: (key: string) => queryParams[key] ?? null,
            has: (key: string) => key in queryParams,
            getAll: () => [],
            keys: Object.keys(queryParams),
          }),
        },
      },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(RunComparisonComponent);
  fixture.detectChanges();
  return fixture;
}

describe("RunComparisonComponent", () => {
  it("renders the guardrail banner when warnings are present", async () => {
    const result = buildResult({
      guardrails: {
        sameAlgorithm: true,
        sameSymbol: false,
        sameWindow: true,
        sameParameters: false,
        warnings: ["Different symbols: SPY vs QQQ"],
      },
    });

    const fixture = await setup(result);
    const banner = (fixture.nativeElement as HTMLElement).querySelector('[role="alert"]');

    expect(banner).not.toBeNull();
    expect(banner?.textContent ?? "").toMatch(/different symbols/i);
  });

  it("renders side-by-side panels with both runs' strategy names and sources", async () => {
    const fixture = await setup();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toContain("ema_crossover");
    expect(text).toContain("lean-sidecar");
    expect(text).toContain("engine");
  });

  it("renders divergence rows when divergences are present", async () => {
    const result = buildResult({
      divergences: [
        {
          category: "FILL_PRICE_DRIFT",
          tradeNumber: 3,
          msUtc: 1_700_000_000_000,
          message: "Fill price differs by $0.05",
          leftFillPrice: 100.0,
          rightFillPrice: 100.05,
        },
      ],
      firstDivergenceMsUtc: 1_700_000_000_000,
    });

    const fixture = await setup(result);
    const rows = (fixture.nativeElement as HTMLElement).querySelectorAll(
      ".divergences tbody tr",
    );

    expect(rows.length).toBe(1);
    const rowText = rows.item(0)?.textContent ?? "";
    expect(rowText).toContain("FILL_PRICE_DRIFT");
    expect(rowText).toContain("Fill price differs by $0.05");
  });

  it("renders an empty-state message when the comparison query returns null", async () => {
    const fixture = await setup(null);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text.toLowerCase()).toContain("not found");
  });

  it("renders the empty state when left query param is missing", async () => {
    const fixture = await setup(buildResult(), { right: "2" });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text.toLowerCase()).toContain("not found");
  });

  it("renders the empty state when right query param is missing", async () => {
    const fixture = await setup(buildResult(), { left: "1" });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text.toLowerCase()).toContain("not found");
  });

  it("renders the empty state when neither query param is present", async () => {
    const fixture = await setup(buildResult(), {});
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text.toLowerCase()).toContain("not found");
  });

  it("passes the parsed integer leftId and rightId to the GraphQL query", async () => {
    const apolloStub = makeApollo(buildResult());

    await TestBed.configureTestingModule({
      imports: [RunComparisonComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: Apollo, useValue: apolloStub },
        {
          provide: ActivatedRoute,
          useValue: {
            queryParamMap: of({
              get: (key: string) => ({ left: "10", right: "20" } as Record<string, string>)[key] ?? null,
              has: () => true,
              getAll: () => [],
              keys: ["left", "right"],
            }),
          },
        },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(RunComparisonComponent);
    fixture.detectChanges();

    expect(apolloStub.watchQuery).toHaveBeenCalledWith(
      expect.objectContaining({ variables: { leftId: 10, rightId: 20 } }),
    );
  });
});
