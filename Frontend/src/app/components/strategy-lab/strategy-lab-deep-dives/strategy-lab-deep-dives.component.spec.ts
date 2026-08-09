import { Component, input, provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { EngineResultData, LeanStatistics } from "../../lean-engine/engine-results/engine-results.component";
import { LeanStatisticsComponent } from "../../lean-engine/lean-statistics/lean-statistics.component";
import { StrategyLabDeepDivesComponent } from "./strategy-lab-deep-dives.component";

@Component({ selector: "app-lean-statistics", template: "Native statistics" })
class LeanStatisticsStubComponent {
  readonly stats = input.required<LeanStatistics>();
}

describe("StrategyLabDeepDivesComponent", () => {
  it("keeps detailed compatibility and native statistics in collapsed disclosures", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabDeepDivesComponent],
      providers: [provideZonelessChangeDetection()],
    }).overrideComponent(StrategyLabDeepDivesComponent, {
      remove: { imports: [LeanStatisticsComponent] },
      add: { imports: [LeanStatisticsStubComponent] },
    }).compileComponents();
    const fixture = TestBed.createComponent(StrategyLabDeepDivesComponent);
    fixture.componentRef.setInput("result", {
      lean_statistics: { portfolio: {}, trade: {}, runtime: {} } as LeanStatistics,
      lean_analysis: [],
      trades: [],
      total_trades: 0,
      validation_analytics: null,
    } as unknown as EngineResultData);
    fixture.componentRef.setInput("parity", {
      status: "diverged",
      reason: "The runs differ.",
      countsByCategory: [{ category: "trade_count", count: 1 }],
      divergences: [{ category: "trade_count", message: "Counts differ." }],
    });
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    const compatibility = root.querySelector<HTMLDetailsElement>("[data-testid='compatibility-deep-dive']");
    const native = root.querySelector<HTMLDetailsElement>("[data-testid='lean-statistics-deep-dive']");
    expect(compatibility?.open).toBe(false);
    expect(native?.open).toBe(false);
    expect(compatibility?.textContent).toContain("Counts differ");
    expect(native?.querySelector("app-lean-statistics")).not.toBeNull();
  });
});
