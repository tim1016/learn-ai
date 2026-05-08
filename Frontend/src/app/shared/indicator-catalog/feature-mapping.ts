/**
 * Maps a (catalog-indicator, params) pick to a backend feature ID.
 *
 * The IC test backend currently understands a small fixed set of feature
 * names (``rsi_14``, ``macd_signal``, etc.). This shim lets the catalog UI
 * present the data-lab indicator vocabulary while still routing runs through
 * the existing ``feature_research`` job. When the backend grows a
 * param-aware ``(kind, params) → feature`` interface we can drop this map
 * and pass the indicator + params through directly.
 */
export interface FeatureMapping {
  indicator: string;
  params: Record<string, number>;
  featureId: string;
  label: string;
}

export const FEATURE_MAPPINGS: readonly FeatureMapping[] = [
  { indicator: 'rsi',  params: { length: 14 }, featureId: 'rsi_14',      label: 'RSI (14)' },
  { indicator: 'macd', params: { fast: 12, slow: 26, signal: 9 }, featureId: 'macd_signal', label: 'MACD Signal' },
  { indicator: 'mom',  params: { length: 5 },  featureId: 'momentum_5m', label: '5-Minute Momentum' },
];

/** Return the backend feature ID for a given (indicator, params) pick, or
 *  null if the combo isn't backend-supported yet. */
export function findFeatureId(
  indicator: string,
  params: Record<string, number>,
): FeatureMapping | null {
  return (
    FEATURE_MAPPINGS.find(
      (m) =>
        m.indicator === indicator &&
        Object.entries(m.params).every(([k, v]) => params[k] === v),
    ) ?? null
  );
}

/** All indicator keys that have at least one supported (params) variant —
 *  used by the catalog to grey-out unsupported indicators. */
export const SUPPORTED_INDICATORS: ReadonlySet<string> = new Set(
  FEATURE_MAPPINGS.map((m) => m.indicator),
);
