import type { HelpDocumentRoute } from '../shared/help/help-documents';
import type { DataSourceProvenance } from '../shared/workspace/data-source-provenance';
import type { StoredStudyType } from './stored-study/stored-study';

/**
 * The Research place's studies (ADR 0084, PRD #3490 R24): each one's address,
 * the question it answers, the constraints it fixes in place of controls, the
 * provider behind it today, what it cannot tell you and where to go next.
 *
 * One list feeds the study list, the study layout and the menu's addresses, so
 * a study cannot be named, routed or sourced two ways.
 */
export type ResearchStudyId =
  | 'feature'
  | 'cross-sectional'
  | 'signal'
  | 'indicator-reliability'
  | 'realized-vs-iv';

/** The served document that explains a study, and the section to open it at. */
export interface StudyHelp {
  readonly route: HelpDocumentRoute;
  readonly anchor?: string;
}

/** Something the study fixes, shown where a control would be so nobody looks for one. */
export interface StudyConstraint {
  readonly label: string;
  readonly value: string;
}

export interface StudyLink {
  readonly label: string;
  readonly route: string;
}

/**
 * `assessed`: the server hands back its own assessment, and the result leads
 * with it. `descriptive`: the result measures and describes; it is never given
 * a pass/fail verdict, because no threshold for one has been decided (R24).
 */
export type StudyResultKind = 'assessed' | 'descriptive';

export interface StudyDefinition {
  readonly id: ResearchStudyId;
  readonly route: string;
  /** The History kind that holds this study's saved runs, and so the one its page links to. */
  readonly history: StoredStudyType;
  readonly question: string;
  /** What the study picks first: one instrument, or a universe of them. */
  readonly subject: 'instrument' | 'universe';
  readonly resultKind: StudyResultKind;
  readonly help: StudyHelp | null;
  /** Fixed by the study. Each is a label, never a control. */
  readonly constraints: readonly StudyConstraint[];
  /** Today's provider for the study, until its lake migration (#3521-#3525) types the real source. */
  readonly source: DataSourceProvenance;
  /** What the study cannot say, in the words a reader needs before trusting a result. */
  readonly limitations: readonly string[];
  readonly nextStep: string;
  readonly nextLinks: readonly StudyLink[];
}

/**
 * What the data-source line says for a study that still reads the provider.
 * The server records no provenance for these results, so every field this does
 * not name is "not recorded". It names only what the study's code fixes, so no
 * field is inferred from the instrument, the window or today's holdings.
 */
function providerSource(
  facts: Pick<DataSourceProvenance, 'sources'> & Partial<DataSourceProvenance>,
): DataSourceProvenance {
  return {
    subject: null,
    requestedWindow: null,
    effectiveWindow: null,
    warmup: null,
    resolution: null,
    adjustment: null,
    sessionPolicy: null,
    dataIdentity: null,
    observedAtMs: null,
    counts: null,
    limitations: [],
    ...facts,
  };
}

export const RESEARCH_STUDIES: readonly StudyDefinition[] = [
  {
    id: 'feature',
    route: '/research/feature',
    history: 'feature-study',
    question: 'Does this indicator predict the next move for this instrument?',
    subject: 'instrument',
    resultKind: 'assessed',
    help: { route: '/data-lab-docs' },
    constraints: [
      { label: 'Session', value: 'extended hours included, fixed' },
      { label: 'Looks ahead', value: '15 minutes, fixed' },
    ],
    source: providerSource({ sources: ['polygon'], adjustment: 'split_adjusted', sessionPolicy: 'extended' }),
    limitations: [
      'Bars include pre-market and after-hours trades, and the indicator runs straight across the overnight gap.',
      'It tests one instrument and one indicator at a time, over the window you pick and nothing before it.',
    ],
    nextStep: 'An indicator that holds up here can be tried as a trading signal, which adds a train and test split and costs.',
    nextLinks: [{ label: 'Open the Signal study', route: '/research/signal' }],
  },
  {
    id: 'cross-sectional',
    route: '/research/cross-sectional',
    history: 'cross-sectional-study',
    question: 'Does an option-implied feature predict what a stock does next, across a whole universe?',
    subject: 'universe',
    resultKind: 'assessed',
    help: null,
    constraints: [
      { label: 'Bars', value: 'daily, fixed' },
      { label: 'Session', value: "the provider's daily bar, fixed" },
    ],
    source: providerSource({
      sources: ['Polygon stock bars', 'Polygon option bars', 'FRED risk-free rate'],
      adjustment: 'split_adjusted',
      resolution: 'daily',
    }),
    limitations: [
      'Implied volatility is rebuilt from the provider’s option bars, so a member with no usable option history is left out of the statistics and listed with its reason.',
      'Only the members counted as valid enter the aggregate; the counts above the table say how many that is.',
    ],
    nextStep: 'Widen or narrow the universe, or change the feature or the target, and run again.',
    nextLinks: [],
  },
  {
    id: 'signal',
    route: '/research/signal',
    history: 'signal-study',
    question: 'Does a trading signal built on this indicator hold up out of sample?',
    subject: 'instrument',
    resultKind: 'assessed',
    help: { route: '/docs/signal-engine-methodology' },
    constraints: [
      { label: 'Session', value: 'extended hours included, fixed' },
      { label: 'Looks ahead', value: '15 bars, fixed' },
    ],
    source: providerSource({ sources: ['polygon'], adjustment: 'split_adjusted', sessionPolicy: 'extended' }),
    limitations: [
      'Bars include pre-market and after-hours trades, and the study needs at least 500 bars in the window.',
      'The train and test split is by bar position, so a short window gives a short test.',
    ],
    nextStep: 'A signal that survives can be written as entry and exit rules and replayed in the Rule sandbox.',
    nextLinks: [{ label: 'Open the Rule sandbox', route: '/spec-strategy' }],
  },
  {
    id: 'indicator-reliability',
    route: '/research/indicator-reliability',
    history: 'indicator-reliability',
    question: 'Is this indicator’s link to what price does next real, or what chance alone produces?',
    subject: 'instrument',
    resultKind: 'assessed',
    help: { route: '/docs/indicator-reliability-methodology', anchor: '1-context-and-scope' },
    constraints: [
      { label: 'Session', value: 'regular trading hours, fixed' },
      { label: 'Bars', value: '1 minute, fixed' },
    ],
    source: providerSource({
      sources: ['polygon'],
      adjustment: 'split_adjusted',
      sessionPolicy: 'regular',
      resolution: '1 minute',
    }),
    limitations: [
      'A lead-in cut short by the provider’s history, the listing date or gaps is not reported, so an early window can start unwarmed.',
      'It tests one instrument at a time. The last bars of the window have no forward return and are left out.',
    ],
    nextStep: 'An indicator with a real link can be run through the Feature study on the same instrument.',
    nextLinks: [{ label: 'Open the Feature study', route: '/research/feature' }],
  },
  {
    id: 'realized-vs-iv',
    route: '/research/realized-vs-iv',
    history: 'realized-vs-iv',
    question: 'Is implied volatility richer or cheaper than the volatility the stock then realizes?',
    subject: 'instrument',
    resultKind: 'descriptive',
    help: null,
    constraints: [
      {
        label: 'Window',
        value: 'the last year of daily bars, or the last month of intraday bars, ending today, fixed',
      },
    ],
    source: providerSource({
      sources: ['Polygon aggregates', 'IV recorder (recorded IV30)', 'Polygon option snapshot (current IV marker)'],
      adjustment: 'split_adjusted',
    }),
    limitations: [
      'Implied volatility comes only from what the recorder stored, matched to bars by exact timestamp; with none stored the study shows realized volatility alone.',
      'The last bars have no forward window, so their forward figures stay unavailable.',
      'The current IV marker is a live snapshot; it is never written into past dates, and a saved run does not keep it.',
    ],
    nextStep: 'Change the instrument, bar size or tenor and run again.',
    nextLinks: [],
  },
];

export function studyDefinition(id: ResearchStudyId): StudyDefinition {
  const found = RESEARCH_STUDIES.find((study) => study.id === id);
  if (found === undefined) throw new Error(`No research study is registered as ${id}.`);
  return found;
}

/** An address the Research place routes to a demonstration: sample data, never a measurement. */
export type ResearchDemoId = 'cross-asset' | 'regimes' | 'edge-score' | 'trade-simulator';

export interface DemoDefinition {
  readonly id: ResearchDemoId;
  readonly route: string;
  readonly summary: string;
}

export const RESEARCH_DEMOS: readonly DemoDefinition[] = [
  {
    id: 'cross-asset',
    route: '/research/demos/cross-asset',
    summary: 'Does an edge survive across SPY, QQQ, IWM and DIA, and across time?',
  },
  {
    id: 'regimes',
    route: '/research/demos/regimes',
    summary: 'Market states found by K-means and a Gaussian HMM.',
  },
  {
    id: 'edge-score',
    route: '/research/demos/edge-score',
    summary: 'How four components combine into one composite score.',
  },
  {
    id: 'trade-simulator',
    route: '/research/demos/trade-simulator',
    summary: 'Pessimistic fills, spread and slippage taken out of a signal’s gross result.',
  },
];

/**
 * The Rule sandbox is the Strategy Spec builder at its present address until
 * its own slice moves it under `/research` (#3518).
 */
export const RULE_SANDBOX = {
  route: '/spec-strategy',
  summary: 'Write entry and exit rules over indicators and replay them on past prices. Nothing built here is deployed.',
} as const;

/** The saved results of every study, in one list: the data plane merges each store's own records (R26). */
export const RESEARCH_HISTORY = {
  route: '/research/history',
  summary:
    'Every saved study and run, from whichever store keeps it. Opening one shows what was saved; it never runs the study again.',
} as const;
