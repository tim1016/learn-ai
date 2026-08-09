import { Component, input, output, provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideRouter } from "@angular/router";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { RunReportComponent } from "../../engine-lab/run-report/run-report.component";
import { StrategyLabResultsComponent } from "./strategy-lab-results.component";

@Component({ selector: "app-engine-run-report", template: "Persisted run evidence" })
class RunReportStubComponent {
  readonly runId = input.required<number>();
  readonly runRefreshed = output<BacktestRunDetail>();
}

describe("StrategyLabResultsComponent", () => {
  it("renders a read-only report with a restore-aware Back to workbench action", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabResultsComponent],
      providers: [provideZonelessChangeDetection(), provideRouter([])],
    }).overrideComponent(StrategyLabResultsComponent, {
      remove: { imports: [RunReportComponent] },
      add: { imports: [RunReportStubComponent] },
    }).compileComponents();

    const fixture = TestBed.createComponent(StrategyLabResultsComponent);
    fixture.componentRef.setInput("id", "91");
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    const back = root.querySelector<HTMLAnchorElement>(".results-page__back");
    expect(back?.textContent).toContain("Back to workbench");
    expect(back?.getAttribute("href")).toContain("/strategy-lab?restoreRun=91");
    expect(root.querySelector("app-engine-run-report")).not.toBeNull();
    expect(root.querySelector("app-strategy-lab-config-rail")).toBeNull();
    expect(root.querySelector("input, select, textarea, button")).toBeNull();
  });

  it("does not attempt to load an invalid persisted run", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabResultsComponent],
      providers: [provideZonelessChangeDetection(), provideRouter([])],
    }).overrideComponent(StrategyLabResultsComponent, {
      remove: { imports: [RunReportComponent] },
      add: { imports: [RunReportStubComponent] },
    }).compileComponents();

    const fixture = TestBed.createComponent(StrategyLabResultsComponent);
    fixture.componentRef.setInput("id", "invalid");
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("Run not found.");
    expect(root.querySelector("app-engine-run-report")).toBeNull();
  });
});
