/* Chart series color tokens (PRD 2026-09-12 data-lab workspace redesign §10).
 *
 * Chart code stores token IDs — never hex, RGB, or arbitrary CSS strings —
 * and resolves `var(--chart-series-*)` only at the Lightweight Charts
 * boundary. The custom properties are registered in `styles/_tokens.scss`
 * (single TV-dark surface today; a future light theme re-registers the same
 * token IDs against its own values).
 *
 * The `hex` literals below duplicate the SCSS values so the contrast spec
 * can compute WCAG ratios without a DOM. `chart-series-color-tokens.spec.ts`
 * reads the `_tokens.scss` source and fails if the two drift, keeping
 * this map honest rather than a second source of truth.
 *
 * Contrast values are against the chart surface `--bg-surface` (#131722) and
 * must stay >= 3:1 (WCAG non-text contrast). */

export type ChartSeriesColorToken =
  | 'series-blue'
  | 'series-sky'
  | 'series-teal'
  | 'series-green'
  | 'series-amber'
  | 'series-orange'
  | 'series-red'
  | 'series-pink'
  | 'series-purple'
  | 'series-violet';

export interface ChartSeriesColorTokenDef {
  readonly id: ChartSeriesColorToken;
  /** Label shown next to the swatch — never color-only identification. */
  readonly label: string;
  /** The CSS custom property this token resolves to. */
  readonly cssVar: string;
  /** Literal dark-surface value from `_tokens.scss` (contrast-spec fixture). */
  readonly hex: string;
}

/** The chart surface series draw against (see trading-chart.component.scss:
 *  `.trading-chart { background: var(--bg-surface) }`). */
export const CHART_SERIES_SURFACE_HEX = '#131722';

const DEFS: readonly ChartSeriesColorTokenDef[] = [
  { id: 'series-blue',   label: 'Blue',   cssVar: '--chart-series-blue',   hex: '#4d8dff' },
  { id: 'series-sky',    label: 'Sky',    cssVar: '--chart-series-sky',    hex: '#29b6f6' },
  { id: 'series-teal',   label: 'Teal',   cssVar: '--chart-series-teal',   hex: '#26c6da' },
  { id: 'series-green',  label: 'Green',  cssVar: '--chart-series-green',  hex: '#26a69a' },
  { id: 'series-amber',  label: 'Amber',  cssVar: '--chart-series-amber',  hex: '#f2ad3d' },
  { id: 'series-orange', label: 'Orange', cssVar: '--chart-series-orange', hex: '#ff6d00' },
  { id: 'series-red',    label: 'Red',    cssVar: '--chart-series-red',    hex: '#ef5350' },
  { id: 'series-pink',   label: 'Pink',   cssVar: '--chart-series-pink',   hex: '#ec407a' },
  { id: 'series-purple', label: 'Purple', cssVar: '--chart-series-purple', hex: '#ab47bc' },
  { id: 'series-violet', label: 'Violet', cssVar: '--chart-series-violet', hex: '#a78bfa' },
];

/** Frozen token registry keyed by token ID. `Object.freeze` alone cannot
 *  lock a Map's internal slots, so the mutating methods are replaced with
 *  throwing stubs — any registry mutation attempt fails loudly. */
const TOKEN_REGISTRY = new Map(DEFS.map(def => [def.id, def]));
for (const method of ['set', 'delete', 'clear'] as const) {
  Object.defineProperty(TOKEN_REGISTRY, method, {
    value: (): never => {
      throw new Error(`CHART_SERIES_COLOR_TOKENS is a frozen read-only registry (${method}() is not allowed)`);
    },
  });
}
Object.freeze(TOKEN_REGISTRY);

export const CHART_SERIES_COLOR_TOKENS: ReadonlyMap<ChartSeriesColorToken, ChartSeriesColorTokenDef> =
  TOKEN_REGISTRY;

/** All token IDs eligible for series assignment, in registry order. */
export const CHART_SERIES_ELIGIBLE_TOKENS: readonly ChartSeriesColorToken[] =
  Object.freeze(DEFS.map(def => def.id));

export function isChartSeriesColorToken(v: unknown): v is ChartSeriesColorToken {
  return typeof v === 'string' && CHART_SERIES_COLOR_TOKENS.has(v as ChartSeriesColorToken);
}

/** Resolve a token to its CSS custom-property reference. */
export function chartSeriesColorVar(token: ChartSeriesColorToken): string {
  const def = CHART_SERIES_COLOR_TOKENS.get(token);
  if (!def) throw new Error(`Unknown chart series color token: ${token}`);
  return `var(${def.cssVar})`;
}
