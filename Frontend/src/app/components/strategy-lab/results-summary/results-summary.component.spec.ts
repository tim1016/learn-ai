import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { RouterTestingModule } from "@angular/router/testing";
import { describe, expect, it } from "vitest";

import type { MetricDocumentationContext } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { ResultsSummaryComponent } from "./results-summary.component";

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

describe("ResultsSummaryComponent — metric documentation links", () => {
  it("carries each metric's own recorded context and run id to its help link, not just Sharpe's", async () => {
    const fixture = await renderSummary({
      runId: 42,
      metricDocumentation: [
        recordedContext("sharpe", "sharpe.lean_native.v1", "lean_native"),
        recordedContext("sortino", "sortino.lean_native.v1", "lean_native"),
        recordedContext("maximum_drawdown", "maximum_drawdown.lean_native.v1", "lean_native"),
      ],
    });

    const root = fixture.nativeElement as HTMLElement;
    const sharpeLink = root.querySelector<HTMLAnchorElement>(
      'a[aria-label="How Sharpe is calculated by the recorded producer"]',
    );
    const sortinoLink = root.querySelector<HTMLAnchorElement>(
      'a[aria-label="How Sortino is calculated by the recorded producer"]',
    );
    const drawdownLink = root.querySelector<HTMLAnchorElement>(
      'a[aria-label="How Max drawdown is calculated by the recorded producer"]',
    );

    expect(sharpeLink?.getAttribute("href")).toContain("variant=sharpe.lean_native.v1");
    expect(sharpeLink?.getAttribute("href")).toContain("run=42");
    expect(sortinoLink?.getAttribute("href")).toContain("variant=sortino.lean_native.v1");
    expect(sortinoLink?.getAttribute("href")).toContain("producer=lean_native");
    expect(sortinoLink?.getAttribute("href")).toContain("run=42");
    expect(drawdownLink?.getAttribute("href")).toContain("variant=maximum_drawdown.lean_native.v1");
    expect(drawdownLink?.getAttribute("href")).toContain("run=42");
  });

  it("falls back to the plain (non-recorded) help trigger for Sortino and Max drawdown when no context is recorded", async () => {
    const fixture = await renderSummary();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('a[aria-label$="by the recorded producer"]')).toBeNull();
    expect(root.querySelector('button[aria-label="How Sortino is calculated"]')).not.toBeNull();
    expect(root.querySelector('button[aria-label="How Max drawdown is calculated"]')).not.toBeNull();
  });
});
