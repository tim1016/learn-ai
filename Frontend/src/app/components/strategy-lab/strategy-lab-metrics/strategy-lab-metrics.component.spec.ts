import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import { StrategyLabMetricsComponent } from "./strategy-lab-metrics.component";

describe("StrategyLabMetricsComponent formatting", () => {
  it("pins currency, number, percentage, missing, and non-finite presentation", async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyLabMetricsComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const component = TestBed.createComponent(StrategyLabMetricsComponent).componentInstance;

    expect(component.formatCurrency(1234.5)).toBe("$1,234.50");
    expect(component.formatNumber(1.234)).toBe("1.23");
    expect(component.formatPercent(0.1234)).toBe("12.34%");
    expect(component.formatCurrency(null)).toBe("—");
    expect(component.formatNumber(Number.NaN)).toBe("—");
    expect(component.formatPercent(Number.POSITIVE_INFINITY)).toBe("∞");
    expect(component.formatNumber(Number.NEGATIVE_INFINITY)).toBe("−∞");
  });
});
