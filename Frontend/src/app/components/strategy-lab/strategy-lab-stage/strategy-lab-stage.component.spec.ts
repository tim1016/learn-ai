import { Component, input, provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { makeRun } from "../testing/run-fixtures";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";
import { StrategyLabStageComponent } from "./strategy-lab-stage.component";

@Component({ selector: "app-strategy-lab-chart", template: `<div data-testid="chart">chart</div>` })
class ChartStubComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<unknown[]>([]);
  readonly equityPoints = input<unknown[]>([]);
}

async function createStageFixture(): Promise<ComponentFixture<StrategyLabStageComponent>> {
  await TestBed.configureTestingModule({
    imports: [StrategyLabStageComponent],
    providers: [provideZonelessChangeDetection()],
  }).overrideComponent(StrategyLabStageComponent, {
    remove: { imports: [StrategyLabChartComponent] },
    add: { imports: [ChartStubComponent] },
  }).compileComponents();

  return TestBed.createComponent(StrategyLabStageComponent);
}

function setStageInputs(
  fixture: ComponentFixture<StrategyLabStageComponent>,
  inputs: Record<string, unknown>,
): void {
  const base = { symbol: "SPY", resolution: "minute", fillMode: "signal_bar_close", engine: "python" };
  for (const [key, value] of Object.entries({ ...base, ...inputs })) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  TestBed.tick();
}

async function renderStage(inputs: Record<string, unknown>): Promise<ComponentFixture<StrategyLabStageComponent>> {
  const fixture = await createStageFixture();
  setStageInputs(fixture, inputs);
  return fixture;
}

describe("StrategyLabStageComponent", () => {
  it("shows the honest placeholder before any run exists", async () => {
    const fixture = await renderStage({ run: null });
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain("Run a validation to populate the equity curve");
    expect(root.querySelector("[data-testid='chart']")).toBeNull();
  });

  it("replaces the placeholder with the chart once a run is loaded", async () => {
    const fixture = await renderStage({ run: makeRun() });
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector("[data-testid='chart']")).not.toBeNull();
    expect(root.textContent).not.toContain("Run a validation to populate the equity curve");
  });

  it("keeps the previous chart mounted and dimmed during a re-run", async () => {
    const run = makeRun();
    const fixture = await renderStage({ run, running: false });
    const root = fixture.nativeElement as HTMLElement;
    const chartBeforeRerun = root.querySelector("[data-testid='chart']");
    expect(chartBeforeRerun).not.toBeNull();

    setStageInputs(fixture, {
      run,
      running: true,
      runStatus: "Running indicators and strategy logic…",
    });

    const chartDuringRerun = root.querySelector("[data-testid='chart']");
    expect(chartDuringRerun).not.toBeNull();
    expect(chartDuringRerun).toBe(chartBeforeRerun);
    expect(root.querySelector(".stage__evidence--stale")).not.toBeNull();
    expect(root.querySelector("[role='status']")?.textContent)
      .toContain("Running indicators and strategy logic…");
  });

  it("renders every report notice", async () => {
    const fixture = await renderStage({ run: makeRun(), notices: ["No strict dual-curve report."] });

    expect((fixture.nativeElement as HTMLElement).textContent).toContain("No strict dual-curve report.");
  });
});
