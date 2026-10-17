export interface ManualChapter {
  readonly id: string;
  readonly title: string;
  readonly description: string;
  readonly group: string;
}

export interface TraderManual {
  readonly title: string;
  readonly introduction: string;
  readonly route: string;
  readonly source: string;
  readonly destination: string;
  readonly destinationLabel: string;
  readonly companion: string;
  readonly companionLabel: string;
  readonly practice: string;
  readonly stages: readonly { title: string; description: string }[];
  readonly chapters: readonly ManualChapter[];
}

export const TRADER_MANUALS: Record<'options' | 'broker', TraderManual> = {
  options: {
    title: 'Your first options backtest',
    introduction: 'Build a small practice setup, replay it against past prices, and learn to explain what happened. Then explore every control at your own pace.',
    route: '/docs/options-backtest-manual',
    source: '/assets/docs/options-backtest-manual.md',
    destination: '/options-backtest',
    destinationLabel: 'Open Options Backtest',
    companion: '/docs/broker-manual',
    companionLabel: 'Broker costs & timing',
    practice: 'Start with one bought option. Your goal is to follow a trade from its entry rule to its result, not to find a winning strategy.',
    stages: [
      { title: 'Build a setup', description: 'Choose what to test' },
      { title: 'Check the data', description: 'Know what can run' },
      { title: 'Replay the days', description: 'Follow the simulation' },
      { title: 'Read the evidence', description: 'Explain the result' },
    ],
    chapters: [
      { id: 'first-run', title: 'Run your first backtest', description: 'A guided exercise with one bought call, from setup to one trade.', group: 'Start here' },
      { id: 'basics', title: 'Understand the basics', description: 'Calls, puts, premium, contracts, legs and simulation.', group: 'Start here' },
      { id: 'dates-and-times', title: 'Choose dates and times', description: 'Entry, exit, expiry, New York time, half days and re-entry cutoff.', group: 'Build your setup' },
      { id: 'legs', title: 'Choose your option legs', description: 'Buy, sell, quantities, weekly expiry, ATM, strike steps and protection.', group: 'Build your setup' },
      { id: 'rules', title: 'Add stops and entry rules', description: 'Momentum, stop loss, re-entry and partial or complete square-off.', group: 'Build your setup' },
      { id: 'costs', title: 'Set cash, slippage and fees', description: 'Costs, price rounding, estimated and per-contract charges.', group: 'Build your setup' },
      { id: 'save-setup', title: 'Review and save a setup', description: 'Read-back, saved setups, copy, import, export and deletion.', group: 'Build your setup' },
      { id: 'data-and-run', title: 'Check data and start a run', description: 'Downloads, available dates, progress, cancel and interrupted jobs.', group: 'Run and understand' },
      { id: 'results', title: 'Read your results', description: 'Profit, win rates, drawdown, daily and monthly tables, skipped sessions.', group: 'Run and understand' },
      { id: 'one-day', title: 'Follow one trade and day', description: 'Timeline, fills, stop levels, marks, events, filters and CSV export.', group: 'Run and understand' },
      { id: 'capital', title: 'Test costs and funding', description: 'Slippage tolerance, cash ladder, collateral and capital evidence.', group: 'Explore further' },
      { id: 'history', title: 'Compare and reuse runs', description: 'History, shared sessions, setting differences and saved results.', group: 'Explore further' },
      { id: 'past-chain', title: 'Explore the Past chain', description: 'Historical contracts, expiry series, prices and missing data.', group: 'Explore further' },
      { id: 'troubleshooting', title: 'Find out why it did not trade', description: 'Refusals, no starting price, missing bars and incomplete closes.', group: 'Keep nearby' },
      { id: 'glossary', title: 'Words and further reading', description: 'Plain-language definitions and links to related manuals.', group: 'Keep nearby' },
    ],
  },
  broker: {
    title: 'Know your broker, costs and timing',
    introduction: 'Learn where your money goes, what an order status means, and which deadlines matter. Follow the journey before using a real account.',
    route: '/docs/broker-manual',
    source: '/assets/docs/broker-manual.md',
    destination: '/brokers/alpaca',
    destinationLabel: 'Open broker accounts',
    companion: '/docs/options-backtest-manual',
    companionLabel: 'Options Backtest manual',
    practice: 'Begin by finding a Paper account and reading its Activity. Opening the account page does not place an order.',
    stages: [
      { title: 'Know the account', description: 'Check the trading mode' },
      { title: 'Understand the cost', description: 'Read cash and fees' },
      { title: 'Follow the order', description: 'Confirm what filled' },
      { title: 'Reconcile afterward', description: 'Match the money' },
    ],
    chapters: [
      { id: 'first-visit', title: 'Your first account visit', description: 'A guided tour of account identity, Home, Activity and Settings.', group: 'Start here' },
      { id: 'providers-and-modes', title: 'Who does what', description: 'Alpaca, IBKR, historical data, Dry Run, Paper, Shadow and Live.', group: 'Start here' },
      { id: 'money', title: 'Understand your money', description: 'Cash, premium, buying power, collateral, commitment and profit.', group: 'Before an order' },
      { id: 'fees', title: 'What gets charged', description: 'Commission, SEC, TAF, CAT, ORF, OCC, rounding and published rates.', group: 'Before an order' },
      { id: 'data-and-account-costs', title: 'Data and account costs', description: 'IBKR subscriptions, margin, borrowing, transfers and account plans.', group: 'Before an order' },
      { id: 'orders', title: 'Follow an order', description: 'Accepted, partial fill, cancelled, rejected, replace and fill evidence.', group: 'During the trade' },
      { id: 'timing', title: 'Know the deadlines', description: 'Session times, 0DTE, entry cutoff, scheduled close and early closes.', group: 'During the trade' },
      { id: 'closing', title: 'Close, stop or reach expiry', description: 'Stop, Flatten, exercise, assignment, DNE and residual positions.', group: 'During the trade' },
      { id: 'settlement', title: 'When money becomes available', description: 'Fee posting, T+1 settlement, deposit holds and withdrawals.', group: 'After the trade' },
      { id: 'reconcile', title: 'Match fees to the account', description: 'Estimated, modeled and observed fees, activity and discrepancies.', group: 'After the trade' },
      { id: 'troubleshooting', title: 'When something looks wrong', description: 'Stale data, held entry, rejected close and missing activity.', group: 'Keep nearby' },
      { id: 'glossary', title: 'Words and official sources', description: 'Definitions, current broker policies and account-specific facts.', group: 'Keep nearby' },
    ],
  },
};
