/** Producer values are authored and validated by Python's generated contract. */
export type MetricProducer = string;

export interface VariableDefinition {
  symbol: string;
  definition: string;
}

export interface ValueState {
  state: string;
  display: string;
  scoring_behavior: string;
}

export interface MetricVariant {
  metric_id: string;
  variant_id: string;
  contract_id: string | null;
  producer: MetricProducer;
  category: string;
  label: string;
  definition: string;
  interpretation: string;
  common_misreadings: string[];
  aliases: string[];
  source_keys: string[];
  search_terms: string[];
  formula_latex: string | null;
  variables: VariableDefinition[];
  input_series: string;
  units: string;
  output_scale: string;
  formatting: string;
  sampling_cadence: string;
  annualization: string | null;
  benchmark: string | null;
  risk_free_rate: string | null;
  cost_treatment: string;
  timezone_convention: string;
  value_states: ValueState[];
  canonical_symbol: string;
  source_reference: string | null;
  validating_tests: string[];
  fixture_or_receipt: string | null;
  numerical_tolerance: string | null;
  results_surfaces: string[];
  verdict_membership: string | null;
  alternative_variant_ids: string[];
}

export interface AnalyticalMetricCatalog {
  catalog_version: number;
  variants: MetricVariant[];
}
