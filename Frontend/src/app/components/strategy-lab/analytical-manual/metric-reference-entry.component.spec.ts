import { fireEvent, render, screen } from "@testing-library/angular";
import { provideRouter } from "@angular/router";
import { describe, expect, it } from "vitest";

import type { MetricVariant } from "./analytical-metric-catalog.models";
import { MetricReferenceEntryComponent } from "./metric-reference-entry.component";

const FORMULA_UNAVAILABLE_VARIANT: MetricVariant = {
  metric_id: "reported_value",
  variant_id: "reported_value.lean_runtime.v1",
  contract_id: null,
    producer: "lean_runtime",
    category: "runtime_snapshot",
  label: "Reported value",
  definition: "A runtime-reported value.",
  interpretation: "Read it as a report, not an independently documented formula.",
  common_misreadings: [],
  aliases: [],
  source_keys: [],
  search_terms: [],
  formula_latex: null,
  variables: [],
  input_series: "LEAN runtime output",
  units: "currency",
  output_scale: "reported value",
  formatting: "two decimal places",
  sampling_cadence: "runtime snapshot",
  annualization: null,
  benchmark: null,
  risk_free_rate: null,
  cost_treatment: "Reported by the producer.",
  timezone_convention: "int64 ms UTC at system boundaries.",
  value_states: [],
  canonical_symbol: "LEAN runtime output",
  source_reference: null,
  validating_tests: [],
  fixture_or_receipt: null,
  numerical_tolerance: null,
  results_surfaces: [],
  verdict_membership: null,
  alternative_variant_ids: [],
};

describe("MetricReferenceEntryComponent", () => {
  it("shows an explicit fallback when the generated contract has no formula", async () => {
    await render(MetricReferenceEntryComponent, {
      inputs: { variant: FORMULA_UNAVAILABLE_VARIANT, alternative: null, runId: null },
      providers: [provideRouter([])],
    });

    fireEvent.click(screen.getByText("Math & contract"));
    expect(screen.getByRole("status").textContent).toContain("does not claim a local formula contract");
  });

  it("renders a display formula and keeps malformed formula text out of the DOM as markup", async () => {
    const { container, rerender } = await render(MetricReferenceEntryComponent, {
      inputs: {
        variant: { ...FORMULA_UNAVAILABLE_VARIANT, formula_latex: "\\frac{r}{\\sigma}" },
        alternative: null,
        runId: null,
      },
      providers: [provideRouter([])],
    });

    fireEvent.click(screen.getByText("Math & contract"));
    expect(container.querySelector(".katex-display")).toBeTruthy();
    await rerender({
      componentInputs: {
        variant: { ...FORMULA_UNAVAILABLE_VARIANT, formula_latex: "\\notACommand{<img src=x>}" },
        alternative: null,
        runId: null,
      },
    });

    expect(container.querySelector("img")).toBeNull();
  });
});
