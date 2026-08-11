import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { RouterTestingModule } from "@angular/router/testing";
import { afterEach, describe, expect, it } from "vitest";

import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { ResultsSummaryComponent } from "./results-summary.component";

afterEach(() => {
  document.body.querySelectorAll(".p-dialog-mask").forEach((element) => element.remove());
});

function result(overrides: Partial<EngineResultData> = {}): EngineResultData {
  return {
    success: true,
    strategy_name: "ema_crossover_signal",
    fill_mode: "next_bar_open",
    initial_cash: 100_000,
    final_equity: 101_200,
    net_profit: 1_200,
    total_fees: 12,
    total_trades: 10,
    winning_trades: 6,
    losing_trades: 4,
    win_rate: 0.6,
    statistics: {
      profit_factor: 1.8,
      sharpe_ratio: 1.1,
      sortino_ratio: 1.4,
      max_drawdown_pct: 0.05,
    },
    lean_statistics: null,
    trades: [],
    log_lines: [],
    ...overrides,
  };
}

async function renderSummary(options: {
  metricDocumentation?: MetricDocumentationContext[];
  runId?: number | null;
} = {}) {
  await TestBed.configureTestingModule({
    imports: [ResultsSummaryComponent, RouterTestingModule],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(ResultsSummaryComponent);
  fixture.componentRef.setInput("result", result());
  fixture.componentRef.setInput("verdict", null);
  fixture.componentRef.setInput("metricDocumentation", options.metricDocumentation ?? []);
  fixture.componentRef.setInput("runId", options.runId ?? null);
  fixture.detectChanges();
  return fixture;
}

function recordedContext(metricId: string, variantId: string, producer: string): MetricDocumentationContext {
  return {
    metricId,
    variantId,
    producer,
    contractId: `${producer}-${metricId}-v1`,
    contractProvenance: "recorded",
  };
}

describe("ResultsSummaryComponent — metric documentation modals", () => {
  it("opens each metric's own recorded guide, not just Sharpe's", async () => {
    const fixture = await renderSummary({
      runId: 42,
      metricDocumentation: [
        recordedContext("sharpe", "sharpe.lean_native.v1", "lean_native"),
        recordedContext("sortino", "lean_native.portfolio.sortino_ratio.v1", "lean_native"),
        recordedContext("maximum_drawdown", "lean_native.portfolio.drawdown.v1", "lean_native"),
      ],
    });

    const root = fixture.nativeElement as HTMLElement;
    const openGuide = (label: string) => {
      root.querySelector<HTMLButtonElement>(`button[aria-label="Open ${label} trader guide"]`)?.click();
      fixture.detectChanges();
    };

    openGuide("Sharpe");
    expect(document.body.textContent).toContain("LEAN-native annual performance above its selected risk-free rate");

    openGuide("Sortino");
    expect(document.body.textContent).toContain("dated LEAN primary-credit rate divided by annualized downside deviation");

    openGuide("Max drawdown");
    expect(document.body.textContent).toContain("Largest Strategy Equity peak-to-trough decline.");
  });

  it("renders an accessible modal trigger for every visible quantity", async () => {
    const fixture = await renderSummary();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelectorAll('button[aria-label$="trader guide"]')).toHaveLength(8);
    expect(root.querySelector('button[aria-label="Open Sortino trader guide"]')).not.toBeNull();
    expect(root.querySelector('button[aria-label="Open Total fees trader guide"]')).not.toBeNull();
  });
});
