import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed, type ComponentFixture } from "@angular/core/testing";
import { ActivatedRoute, convertToParamMap } from "@angular/router";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";
import type { CompareResponse } from "../../models/compare-response";
import { RunsCompareService } from "../../services/runs-compare.service";
import { RunsCompareComponent } from "./runs-compare.component";

function emptyTradeDiff(): CompareResponse["trade_diff"] {
  return { matched_pairs: [], python_only: [], lean_only: [], first_divergence: null };
}

function emptyDeltas(): CompareResponse["summary_deltas"] {
  const z = { left: 0, right: 0, delta: 0 };
  return {
    total_trades: z,
    total_pnl: z,
    total_fees: z,
    win_rate: z,
    max_drawdown: z,
    sharpe: z,
  };
}

function emptyRunLinks(): CompareResponse["raw_run_links"] {
  return {
    left: { manifest_path: null, log_path: null, staged_zip_sha256: null },
    right: { manifest_path: null, log_path: null, staged_zip_sha256: null },
  };
}

function buildResponse(over: Partial<CompareResponse> = {}): CompareResponse {
  return {
    left: {
      id: 1,
      engine: "PYTHON",
      data_policy: null,
      summary: { total_trades: 7, total_pnl: 421.5, total_fees: 0, win_rate: 0.571, max_drawdown: -15.2, sharpe: 1.42 },
      starting_cash: 100000,
      commission_per_order: "0.00",
      fill_mode: "signal_bar_close",
      brokerage_policy: "algorithm_default",
      strategy_identity: { kind: "python_registry", name: "spy_ema_crossover", sha256: null },
    },
    right: {
      id: 2,
      engine: "LEAN",
      data_policy: null,
      summary: { total_trades: 7, total_pnl: 419.8, total_fees: 0, win_rate: 0.571, max_drawdown: -15.2, sharpe: 0 },
      starting_cash: 100000,
      commission_per_order: "0.00",
      fill_mode: "signal_bar_close",
      brokerage_policy: "algorithm_default",
      strategy_identity: { kind: "lean_template", name: "ema_crossover", sha256: "abc123" },
    },
    compatible: true,
    mismatches: [],
    informational_differences: [],
    summary_deltas: emptyDeltas(),
    trade_diff: emptyTradeDiff(),
    first_divergence: null,
    state_trace_available: false,
    raw_run_links: emptyRunLinks(),
    ...over,
  };
}

function makeSvcStub(response: CompareResponse | null) {
  return {
    getCompare: vi.fn().mockReturnValue(response === null ? of() : of(response)),
  };
}

async function setup(
  response: CompareResponse | null = buildResponse(),
  queryParams: Record<string, string> = { left: "1", right: "2" },
): Promise<ComponentFixture<RunsCompareComponent>> {
  await TestBed.configureTestingModule({
    imports: [RunsCompareComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: RunsCompareService, useValue: makeSvcStub(response) },
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { queryParamMap: convertToParamMap(queryParams) } },
      },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(RunsCompareComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

describe("RunsCompareComponent", () => {
  it('renders "Comparable" header + sub-claims when compatible=true', async () => {
    const fixture = await setup();
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector(".verdict")?.textContent?.trim()).toBe("Comparable");
    expect(el.querySelectorAll(".claims li").length).toBe(3);
  });

  it("renders mismatch list when compatible=false", async () => {
    const fixture = await setup(
      buildResponse({ compatible: false, mismatches: ["starting_cash", "strategy_bars"] }),
    );
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toContain("Not comparable");
    expect(text).toContain("starting_cash");
    expect(text).toContain("strategy_bars");
  });

  it("omits the State Trace section from the DOM when state_trace_available=false", async () => {
    const fixture = await setup(buildResponse({ state_trace_available: false }));
    const stateTrace = (fixture.nativeElement as HTMLElement).querySelector(".state-trace");

    expect(stateTrace).toBeNull();
  });

  it("renders the first-divergence callout when present", async () => {
    const fixture = await setup(
      buildResponse({
        first_divergence: {
          trade_index: 2,
          what: "exit_price_delta",
          category: "fill_price_drift",
          left_value: "421.50",
          right_value: "421.52",
        },
      }),
    );
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toContain("First divergence");
    expect(text).toContain("fill_price_drift");
    expect(text).toContain("421.50");
    expect(text).toContain("421.52");
  });

  it("renders summary cards with deltas", async () => {
    const fixture = await setup(
      buildResponse({
        summary_deltas: {
          total_trades: { left: 7, right: 7, delta: 0 },
          total_pnl: { left: 421.5, right: 419.8, delta: -1.7 },
          total_fees: { left: 0, right: 0, delta: 0 },
          win_rate: { left: 0.571, right: 0.571, delta: 0 },
          max_drawdown: { left: -15.2, right: -15.2, delta: 0 },
          sharpe: { left: 1.42, right: 0, delta: -1.42 },
        },
      }),
    );
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toContain("Trades");
    expect(text).toContain("Net P&L");
    expect(text).toContain("Sharpe");
  });

  it("renders the empty state when the query params are missing", async () => {
    const fixture = await setup(buildResponse(), {});
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";

    expect(text).toMatch(/query parameters/i);
  });
});
