export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'features/experiments'`). */
  readonly path: string;
  /** Short label for the sub-nav tab. */
  readonly label: string;
}

/**
 * The pages the Research Lab shell still hosts, each waiting for the slice
 * that moves it: Strategy Runs (#3518), History (#3519), instrument headlines
 * (#3501) and the Recency Chart (#3508). The five studies live under
 * `/research`; the two data checks are reached from Operations and need no tab.
 */
export const RESEARCH_LAB_NAV: readonly NavItem[] = [
  { path: 'features/experiments', label: 'Experiments' },
  { path: 'signals/history', label: 'Signal History' },
  { path: 'market/news', label: 'Ticker News' },
  { path: 'backtests/strategy-runs', label: 'Strategy Runs' },
  { path: 'backtests/recency-chart', label: 'Recency Chart' },
];
