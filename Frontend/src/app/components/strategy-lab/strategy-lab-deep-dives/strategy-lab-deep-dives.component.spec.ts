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
  it("keeps evidence controls in the rail and opens selected detail in a drawer", async () => {
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
    const compatibility = [...root.querySelectorAll<HTMLButtonElement>(".evidence-menu__item")]
      .find((button) => button.textContent?.includes("Compatibility evidence"));
    expect(compatibility?.getAttribute("aria-haspopup")).toBe("dialog");
    expect(root.querySelector("details")).toBeNull();
    expect(root.textContent).toContain("Native LEAN statistics");

    compatibility?.click();
    fixture.detectChanges();
    await fixture.whenStable();

    expect(document.body.textContent).toContain("Counts differ");
  });
});
