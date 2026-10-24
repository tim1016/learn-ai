export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'backtests/recency-chart'`). */
  readonly path: string;
  /** Short label for the sub-nav tab. */
  readonly label: string;
}

/**
 * The page the Research Lab shell still hosts, waiting for the slice that
 * moves it: the Recency Chart (#3508). Instrument headlines live under the
 * instrument (`/markets/:symbol/chart/news`), the five studies under
 * `/research`, every saved result and spec-run record in the Research History
 * under `/research/history`, and the two data checks are reached from
 * Operations, so none of them needs a tab.
 */
export const RESEARCH_LAB_NAV: readonly NavItem[] = [
  { path: 'backtests/recency-chart', label: 'Recency Chart' },
];
