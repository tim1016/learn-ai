export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'build/validate'`). */
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
    label: 'Build',
    items: [
      { path: 'build/validate', label: 'Validate' },
      { path: 'build/indicator-reliability', label: 'Reliability' },
      { path: 'build/signal-engine', label: 'Signal Engine' },
    ],
  },
  {
    label: 'Inspect',
    items: [
      { path: 'inspect/cross-sectional', label: 'Cross-Sectional' },
      { path: 'inspect/data-divergence', label: 'Data Divergence' },
      { path: 'inspect/pre-flight', label: 'Pre-flight' },
      { path: 'inspect/strategy-runs', label: 'Strategy Runs' },
      { path: 'inspect/experiments', label: 'Experiments' },
      { path: 'inspect/signal-history', label: 'Signal History' },
    ],
  },
];
