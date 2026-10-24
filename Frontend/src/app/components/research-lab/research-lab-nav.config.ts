import type { HelpDocumentRoute } from '../../shared/help/help-documents';

/** The served document that explains a study, and the section to open it at. */
export interface StudyHelp {
  readonly route: HelpDocumentRoute;
  readonly anchor?: string;
}

export interface NavItem {
  /** Path relative to `/research-lab` (e.g. `'features/validate'`). */
  readonly path: string;
  /** Short label for the sub-nav tab. */
  readonly label: string;
  /** The "How this works" document shown in the header while the study is open. */
  readonly help?: StudyHelp;
}

export interface NavGroup {
  readonly label: string;
  readonly items: readonly NavItem[];
}

export const RESEARCH_LAB_NAV: readonly NavGroup[] = [
  {
    label: 'Features',
    items: [
      { path: 'features/validate', label: 'Validate', help: { route: '/data-lab-docs' } },
      { path: 'features/cross-sectional', label: 'Cross-Sectional' },
      { path: 'features/experiments', label: 'Experiments' },
    ],
  },
  {
    label: 'Signals',
    items: [
      { path: 'signals/engine', label: 'Signal Engine', help: { route: '/docs/signal-engine-methodology' } },
      { path: 'signals/history', label: 'Signal History' },
    ],
  },
  {
    label: 'Market',
    items: [{ path: 'market/news', label: 'Ticker News' }],
  },
  {
    label: 'Backtests',
    items: [
      {
        path: 'backtests/reliability',
        label: 'Reliability',
        // The section the page's own Methodology drawer opens at.
        help: { route: '/docs/indicator-reliability-methodology', anchor: '1-context-and-scope' },
      },
      { path: 'backtests/strategy-runs', label: 'Strategy Runs' },
    ],
  },
];
