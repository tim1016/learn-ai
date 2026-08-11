import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, convertToParamMap } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of, throwError } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type { MetricVariant } from "./analytical-metric-catalog.models";
import {
  StrategyLabAnalyticalManualComponent,
  filterMetricVariants,
} from "./strategy-lab-analytical-manual.component";
import { resolveMetricContext } from "./metric-context.util";

const VARIANTS: MetricVariant[] = [
  {
    metric_id: "sharpe", variant_id: "sharpe.platform.v1", contract_id: "platform-sharpe-v1",
    producer: "platform", category: "statistical_confidence", label: "Sharpe ratio", definition: "Platform definition.", interpretation: "Platform interpretation.",
    common_misreadings: [], aliases: [], source_keys: [], search_terms: [], formula_latex: "S", variables: [], input_series: "Platform returns",
    units: "ratio", output_scale: "ratio", formatting: "two decimals", sampling_cadence: "daily", annualization: null,
    benchmark: null, risk_free_rate: null, cost_treatment: "Recorded", timezone_convention: "UTC", value_states: [],
    canonical_symbol: "platform.py", source_reference: null, validating_tests: [], fixture_or_receipt: null,
    numerical_tolerance: null, results_surfaces: [], verdict_membership: null, alternative_variant_ids: ["sharpe.lean_native.v1"],
  },
  {
    metric_id: "sharpe", variant_id: "sharpe.lean_native.v1", contract_id: "lean-statistics-oracle-v1",
    producer: "lean_native", category: "statistical_confidence", label: "Sharpe ratio", definition: "LEAN definition.", interpretation: "LEAN interpretation.",
    common_misreadings: [], aliases: [], source_keys: [], search_terms: [], formula_latex: "S", variables: [], input_series: "LEAN returns",
    units: "ratio", output_scale: "ratio", formatting: "two decimals", sampling_cadence: "daily", annualization: null,
    benchmark: null, risk_free_rate: null, cost_treatment: "Recorded", timezone_convention: "UTC", value_states: [],
    canonical_symbol: "lean.py", source_reference: null, validating_tests: [], fixture_or_receipt: null,
    numerical_tolerance: null, results_surfaces: [], verdict_membership: null, alternative_variant_ids: ["sharpe.platform.v1"],
  },
];

describe("resolveMetricContext", () => {
  it("selects the exact producer-specific variant from a Results link", () => {
    const resolution = resolveMetricContext(VARIANTS, {
      metricId: "sharpe", variantId: "sharpe.lean_native.v1", producer: "lean_native", contractId: "lean-statistics-oracle-v1",
    });

    expect(resolution).toMatchObject({ variant: { variant_id: "sharpe.lean_native.v1" }, warning: null });
  });

  it("trims the producer before preserving a requested variant", () => {
    const resolution = resolveMetricContext(VARIANTS, {
      metricId: "sharpe", variantId: "sharpe.lean_native.v1", producer: " lean_native ", contractId: "lean-statistics-oracle-v1",
    });

    expect(resolution).toMatchObject({ variant: { variant_id: "sharpe.lean_native.v1" }, warning: null });
  });

  it("falls back visibly for unknown metric and unknown producer context", () => {
    const unknownMetric = resolveMetricContext(VARIANTS, {
      metricId: "unknown_metric", variantId: null, producer: null, contractId: null,
    });
    const unknownProducer = resolveMetricContext(VARIANTS, {
      metricId: "sharpe", variantId: null, producer: "invented_producer", contractId: null,
    });

    expect(unknownMetric.variant.variant_id).toBe("sharpe.platform.v1");
    expect(unknownMetric.warning).toContain("not documented");
    expect(unknownProducer.variant.variant_id).toBe("sharpe.platform.v1");
    expect(unknownProducer.warning).toContain("producer");
  });

  it("warns when a contextual URL names a stale calculation contract", () => {
    const resolution = resolveMetricContext(VARIANTS, {
      metricId: "sharpe",
      variantId: "sharpe.platform.v1",
      producer: "platform",
      contractId: "platform-sharpe-v0",
    });

    expect(resolution.variant.variant_id).toBe("sharpe.platform.v1");
    expect(resolution.warning).toContain("contract");
  });

  it("composes search, producer, category, and current-run filters without deriving a value", () => {
    const base = {
      search: "sharpe",
      category: "statistical_confidence" as const,
      producer: "lean_native",
      contextualVariantId: "sharpe.lean_native.v1",
    };

    expect(filterMetricVariants(VARIANTS, { ...base, usedByThisRun: false })).toEqual([VARIANTS[1]]);
    expect(filterMetricVariants(VARIANTS, { ...base, usedByThisRun: true })).toEqual([VARIANTS[1]]);
    expect(filterMetricVariants(VARIANTS, { ...base, contextualVariantId: "sharpe.platform.v1", usedByThisRun: true })).toEqual([]);
  });

});

async function renderManual(options: {
  queryParams: Record<string, string>;
  metricDocumentation?: { metricId: string; variantId: string; producer: string; contractId: string | null; contractProvenance: string }[] | null;
  queryError?: Error;
}) {
  const params = convertToParamMap(options.queryParams);
  await TestBed.configureTestingModule({
    imports: [StrategyLabAnalyticalManualComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: ActivatedRoute, useValue: { queryParamMap: of(params), snapshot: { queryParamMap: params } } },
      {
        provide: Apollo,
        useValue: {
          query: vi.fn(() =>
            options.queryError
              ? throwError(() => options.queryError)
              : of({ data: { backtestRun: { metricDocumentation: options.metricDocumentation ?? null } } }),
          ),
        },
      },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabAnalyticalManualComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

describe("StrategyLabAnalyticalManualComponent — used-by-this-run trust boundary", () => {
  it("does not claim a hand-authored URL's variant was used by the run when the run's receipt disagrees", async () => {
    const fixture = await renderManual({
      queryParams: { run: "42", metric: "sharpe", variant: "sharpe.lean_native.v1", producer: "lean_native" },
      metricDocumentation: [
        {
          metricId: "sharpe",
          variantId: "sharpe.platform.v1",
          producer: "platform",
          contractId: "platform-sharpe-v1",
          contractProvenance: "recorded",
        },
      ],
    });

    expect(fixture.componentInstance.hasRunContext()).toBe(false);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).not.toContain("Used by this run");
  });

  it("claims used-by-this-run only once the run's real receipt names the selected variant", async () => {
    const fixture = await renderManual({
      queryParams: { run: "42", metric: "sharpe", variant: "sharpe.lean_native.v1", producer: "lean_native" },
      metricDocumentation: [
        {
          metricId: "sharpe",
          variantId: "sharpe.lean_native.v1",
          producer: "lean_native",
          contractId: "lean-statistics-oracle-v1",
          contractProvenance: "recorded",
        },
      ],
    });

    expect(fixture.componentInstance.hasRunContext()).toBe(true);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("Used by this run");
  });

  it("does not throw or claim used-by-this-run when the run receipt query fails", async () => {
    const fixture = await renderManual({
      queryParams: { run: "42", metric: "sharpe", variant: "sharpe.lean_native.v1", producer: "lean_native" },
      queryError: new Error("network error"),
    });

    // resource().value() throws once the resource settles into an error
    // state; hasRunContext must guard on .error() first rather than
    // propagate that throw into a transient backend hiccup crashing the page.
    expect(() => fixture.componentInstance.hasRunContext()).not.toThrow();
    expect(fixture.componentInstance.hasRunContext()).toBe(false);
    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).not.toContain("Used by this run");
  });
});
