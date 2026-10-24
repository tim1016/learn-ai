export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'market/news'`). */
  readonly path: string;
  /** Short label for the sub-nav tab. */
  readonly label: string;
}

/**
 * The pages the Research Lab shell still hosts, each waiting for the slice
 * that moves it: instrument headlines (#3501) and the Recency Chart (#3508).
 * The five studies live under `/research`, every saved result in the Research
 * History; the two data checks are reached from Operations and need no tab.
 */
export const RESEARCH_LAB_NAV: readonly NavItem[] = [
  { path: 'market/news', label: 'Ticker News' },
  { path: 'backtests/recency-chart', label: 'Recency Chart' },
];
