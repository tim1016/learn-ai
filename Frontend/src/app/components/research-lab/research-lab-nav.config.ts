export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'features/validate'`). */
  readonly path: string;
  /** Short label for the sub-nav tab. */
  readonly label: string;
}

export interface NavGroup {
  readonly label: string;
  readonly items: readonly NavItem[];
}

export const RESEARCH_LAB_NAV: readonly NavGroup[] = [
  {
    label: 'Features',
    items: [
      { path: 'features/validate', label: 'Validate' },
      { path: 'features/cross-sectional', label: 'Cross-Sectional' },
      { path: 'features/experiments', label: 'Experiments' },
    ],
  },
  {
    label: 'Signals',
    items: [
      { path: 'signals/engine', label: 'Signal Engine' },
      { path: 'signals/history', label: 'Signal History' },
    ],
  },
  {
    label: 'Backtests',
    items: [
      { path: 'backtests/reliability', label: 'Reliability' },
      { path: 'backtests/strategy-runs', label: 'Strategy Runs' },
    ],
  },
];
