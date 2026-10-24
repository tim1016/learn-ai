/**
 * Every document the app serves, listed once.
 *
 * The top-bar help index projects this list, the window title of a document
 * page reads from it, and a page's "How this works" link names one of its
 * routes, so a document cannot be reachable from one place and missing from
 * another. A new document is one entry here plus its route in `app.routes.ts`.
 *
 * Routes and fragments are the documents' public addresses (bookmarks, the
 * served-copy contract); keep them as they are.
 */
export interface HelpDocument {
  /** The single display name for every projection: window title and index row. */
  readonly title: string;
  /** Router path of the document's page. */
  readonly route: string;
  /** One line saying what a reader gets from the document. */
  readonly description: string;
}

export const HELP_DOCUMENTS = [
  {
    title: 'Options Backtest Manual',
    route: '/docs/options-backtest-manual',
    description: 'Build a practice setup, replay it on past prices and explain the result.',
  },
  {
    title: 'Broker Costs & Timing',
    route: '/docs/broker-manual',
    description: 'Where the money goes, what an order status means and which deadlines matter.',
  },
  {
    title: 'Bot Business Logic Manual',
    route: '/docs/bot-business-manual',
    description: 'What a bot does, the checks around it and the evidence it leaves.',
  },
  {
    title: 'Architecture Manual',
    route: '/docs/architecture-manual',
    description: 'How the system is put together, who may touch money and where the design is weakest.',
  },
  {
    title: 'Strategy Docs',
    route: '/strategy-docs',
    description: 'The Momentum RSI + Stochastic strategy: its rules, parameters and pitfalls.',
  },
  {
    title: 'Strategy Lab Manual',
    route: '/strategy-lab/docs',
    description: 'Read a run’s evidence first, then the metric definitions and engine assumptions.',
  },
  {
    title: 'Indicator Reference',
    route: '/data-lab-docs',
    description: 'Every Data Lab indicator: its formula, its conditions and how to read it.',
  },
  {
    title: 'Pipeline Docs',
    route: '/data-quality-docs',
    description: 'How raw vendor bars are cleaned into indicator-ready data.',
  },
  {
    title: 'Indicator Reliability',
    route: '/docs/indicator-reliability-methodology',
    description: 'The statistics behind the reliability page’s confidence score and decision cells.',
  },
  {
    title: 'Signal Engine',
    route: '/docs/signal-engine-methodology',
    description: 'Signal construction, walk-forward validation and Sharpe inference.',
  },
  {
    title: 'Golden Search Manual',
    route: '/docs/golden-search-manual',
    description: 'The path from a search plan to a final test, one decision checkpoint at a time.',
  },
  {
    title: 'Golden Search Charts',
    route: '/docs/golden-search-guide',
    description: 'What each Golden Search chart answers and how to read it.',
  },
  {
    title: 'Legal Notices',
    route: '/legal/notices',
    description: 'Open-source notices and attributions the app must publish.',
  },
] as const satisfies readonly [HelpDocument, ...HelpDocument[]];

/** The route of a document in `HELP_DOCUMENTS`; a typo in a link is a compile error. */
export type HelpDocumentRoute = (typeof HELP_DOCUMENTS)[number]['route'];

/**
 * The address a document opens at: its route, the query it reads and the
 * section to scroll to. A plain address, not a router link: every help link
 * opens in a new browser tab, which loads the app afresh at that address.
 */
export function helpDocumentUrl(
  route: HelpDocumentRoute,
  options: { readonly anchor?: string; readonly query?: Readonly<Record<string, string | undefined>> } = {},
): string {
  const query = new URLSearchParams(
    Object.entries(options.query ?? {}).flatMap(([key, value]) => (value === undefined ? [] : [[key, value]])),
  ).toString();
  const section = options.anchor === undefined ? '' : `#${encodeURIComponent(options.anchor)}`;
  return `${route}${query === '' ? '' : `?${query}`}${section}`;
}
