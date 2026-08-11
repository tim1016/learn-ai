import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { RouterTestingModule } from "@angular/router/testing";
import { afterEach, describe, expect, it } from "vitest";

import { MetricHelpModalComponent } from "./metric-help-modal.component";

afterEach(() => {
  document.body.querySelectorAll(".p-dialog-mask").forEach((element) => element.remove());
});

async function renderModal(options: {
  metricId?: string;
  variantId?: string;
  label?: string;
  context?: {
    metricId: string;
    variantId: string;
    producer: string;
    contractId: string | null;
    contractProvenance: string;
  } | null;
} = {}) {
  await TestBed.configureTestingModule({
    imports: [MetricHelpModalComponent, RouterTestingModule],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(MetricHelpModalComponent);
  fixture.componentRef.setInput("metricId", options.metricId ?? "sharpe");
  fixture.componentRef.setInput("variantId", options.variantId ?? null);
  fixture.componentRef.setInput("label", options.label ?? "Sharpe");
  fixture.componentRef.setInput("context", options.context ?? null);
  fixture.detectChanges();
  return fixture;
}

describe("MetricHelpModalComponent", () => {
  it("opens the complete trader guide and keeps calculation and developer views available", async () => {
    const fixture = await renderModal();

    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button")?.click();
    fixture.detectChanges();

    expect(document.body.textContent).toContain("Annualized average return per unit of observed return variability.");
    expect(document.body.textContent).toContain("Math & contract");
    expect(document.body.textContent).toContain("Developer reference");
    expect(document.body.textContent).toContain("Open this quantity in the full Strategy Lab manual");
  });

  it("resolves a persisted LEAN quantity to its exact backend-recorded variant", async () => {
    const fixture = await renderModal({
      metricId: "maximum_drawdown",
      label: "Max drawdown",
      context: {
        metricId: "maximum_drawdown",
        variantId: "lean_native.portfolio.drawdown.v1",
        producer: "lean_native",
        contractId: "lean-native-statistics-oracle-v1",
        contractProvenance: "recorded",
      },
    });

    expect(fixture.componentInstance.variant().variant_id).toBe("lean_native.portfolio.drawdown.v1");
    expect(fixture.componentInstance.variant().producer).toBe("lean_native");
  });

  it("opens an exact native-statistics variant without duplicating its authored content", async () => {
    const fixture = await renderModal({
      metricId: undefined,
      variantId: "lean_native.portfolio.alpha.v1",
      label: "Alpha",
    });

    expect(fixture.componentInstance.variant().metric_id).toBe("lean_portfolio_alpha");
    expect(fixture.componentInstance.variant().interpretation).toContain("differentiated strategy behavior");
  });
});
