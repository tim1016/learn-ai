import { describe, expect, it } from "vitest";

import type { MetricVariant } from "./analytical-metric-catalog.models";
import { resolveMetricContext } from "./strategy-lab-analytical-manual.component";

const VARIANTS: MetricVariant[] = [
  {
    metric_id: "sharpe", variant_id: "sharpe.platform.v1", contract_id: "platform-sharpe-v1",
    producer: "platform", label: "Sharpe ratio", definition: "Platform definition.", interpretation: "Platform interpretation.",
    common_misreadings: [], aliases: [], search_terms: [], formula_latex: "S", variables: [], input_series: "Platform returns",
    units: "ratio", output_scale: "ratio", formatting: "two decimals", sampling_cadence: "daily", annualization: null,
    benchmark: null, risk_free_rate: null, cost_treatment: "Recorded", timezone_convention: "UTC", value_states: [],
    canonical_symbol: "platform.py", source_reference: null, validating_tests: [], fixture_or_receipt: null,
    numerical_tolerance: null, results_surfaces: [], verdict_membership: null, alternative_variant_ids: ["sharpe.lean_native.v1"],
  },
  {
    metric_id: "sharpe", variant_id: "sharpe.lean_native.v1", contract_id: "lean-statistics-oracle-v1",
    producer: "lean_native", label: "Sharpe ratio", definition: "LEAN definition.", interpretation: "LEAN interpretation.",
    common_misreadings: [], aliases: [], search_terms: [], formula_latex: "S", variables: [], input_series: "LEAN returns",
    units: "ratio", output_scale: "ratio", formatting: "two decimals", sampling_cadence: "daily", annualization: null,
    benchmark: null, risk_free_rate: null, cost_treatment: "Recorded", timezone_convention: "UTC", value_states: [],
    canonical_symbol: "lean.py", source_reference: null, validating_tests: [], fixture_or_receipt: null,
    numerical_tolerance: null, results_surfaces: [], verdict_membership: null, alternative_variant_ids: ["sharpe.platform.v1"],
  },
];

describe("resolveMetricContext", () => {
  it("selects the exact producer-specific variant from a Results link", () => {
    const resolution = resolveMetricContext(VARIANTS, {
      metricId: "sharpe", variantId: "sharpe.lean_native.v1", producer: "lean_native",
    });

    expect(resolution).toMatchObject({ variant: { variant_id: "sharpe.lean_native.v1" }, warning: null });
  });

  it("falls back visibly for unknown metric and unknown producer context", () => {
    const unknownMetric = resolveMetricContext(VARIANTS, {
      metricId: "unknown_metric", variantId: null, producer: null,
    });
    const unknownProducer = resolveMetricContext(VARIANTS, {
      metricId: "sharpe", variantId: null, producer: "invented_producer",
    });

    expect(unknownMetric.variant.variant_id).toBe("sharpe.platform.v1");
    expect(unknownMetric.warning).toContain("not documented");
    expect(unknownProducer.variant.variant_id).toBe("sharpe.platform.v1");
    expect(unknownProducer.warning).toContain("producer");
  });
});
