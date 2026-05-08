export interface ToleranceInfo {
  atol: number;
  rtol: number;
  note: string;
}

export interface FixtureSummary {
  id: string;
  name: string;
  category: string;
  canonical_module: string;
  canonical_callable: string;
  reference_kind: string;
  is_certified: boolean;
  status: string;
  active_version: number;
  tolerance: ToleranceInfo;
}

export interface ValidationSummary {
  generated_at: string;
  passed: number;
  failed: number;
  errors: number;
  status: string;
}

export interface GoldenFixturesCatalog {
  fixtures: FixtureSummary[];
  validation: ValidationSummary | null;
}

export const CATEGORY_LABELS: Record<string, string> = {
  'options-pricing': 'Options Pricing',
  'implied-volatility': 'Implied Volatility',
  'realized-volatility': 'Realized Volatility',
  'engine-statistics': 'Engine Statistics',
  indicators: 'Indicators',
  'research-primitives': 'Research Primitives',
  'indicator-reliability': 'Indicator Reliability',
};

export const REFERENCE_KIND_LABELS: Record<string, string> = {
  cross_engine: 'Cross-Engine',
  external_reference: 'External Reference',
  literature_formula: 'Literature Formula',
  hand_computed: 'Hand Computed',
  vendor_observed: 'Vendor Observed',
  internal_regression: 'Regression Pinned',
};
