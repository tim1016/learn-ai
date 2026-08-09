import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import metricGolden from "@repo-contracts/fixtures/strategy-metric-help-golden-v1.json";

import { STRATEGY_METRIC_HELP } from "../../lean-engine/metric-grade.util";
import { MetricHelpPopoverComponent } from "./metric-help-popover.component";

describe("MetricHelpPopoverComponent", () => {
  it("opens an accessible inline definition with formula, source, and docs link", async () => {
    await TestBed.configureTestingModule({
      imports: [MetricHelpPopoverComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(MetricHelpPopoverComponent);
    fixture.componentRef.setInput("metric", STRATEGY_METRIC_HELP.sharpe);
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    root.querySelector<HTMLButtonElement>("button")?.click();
    fixture.detectChanges();

    const popover = root.querySelector<HTMLElement>("[role='dialog']");
    expect(popover?.textContent).toContain("Annualized return earned");
    expect(popover?.textContent).toContain("√252");
    expect(popover?.textContent).toContain("statistics.py::_sharpe");
    expect(popover?.querySelector<HTMLAnchorElement>("a")?.getAttribute("href"))
      .toBe("/strategy-lab/docs#sharpe");
  });

  it("pins every metric to a visible canonical source and Strategy Lab docs anchor", () => {
    for (const metric of Object.values(STRATEGY_METRIC_HELP)) {
      expect(metric.source).toContain("PythonDataService/app/engine/results/statistics.py::");
      expect(metric.docAnchor).toBe(`/strategy-lab/docs#${metric.id}`);
      expect(metric.formula.trim().length).toBeGreaterThan(4);
    }
    expect(Object.keys(STRATEGY_METRIC_HELP).sort())
      .toEqual(Object.keys(metricGolden.expected).sort());
    expect(metricGolden.absolute_tolerance).toBe(1e-12);
    expect(metricGolden.relative_tolerance).toBe(0);
  });
});
