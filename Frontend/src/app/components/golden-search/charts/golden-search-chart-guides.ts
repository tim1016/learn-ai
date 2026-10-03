import type { HighlightTarget } from './golden-search-chart-spec';

/** Every chart with a guide section; the id is that section's anchor in the Golden Search guide. */
export type GoldenSearchChartId =
  | 'candidate-cards'
  | 'equity-and-fall'
  | 'side-by-side'
  | 'neighbor-tornado'
  | 'cost-stress'
  | 'concentration-curve'
  | 'without-best';

export interface WalkthroughStep {
  /** The step as the guide's "How to read it" list words it (the contract spec holds the step counts together). */
  readonly text: string;
  /** What the step lights up on the chart; null when it reads the panel as a whole. */
  readonly target: HighlightTarget | null;
}

export interface ChartGuide {
  /** The panel title, and the guide section's heading. */
  readonly title: string;
  /** The one-line question the panel answers. */
  readonly question: string;
  readonly steps: readonly WalkthroughStep[];
}

export const CHART_GUIDES: Readonly<Record<GoldenSearchChartId, ChartGuide>> = {
  'candidate-cards': {
    title: 'Candidate cards',
    question: 'Which settings are you comparing, and how did each do on the development data?',
    steps: [
      { text: 'Find the selected card. The rest of the page explains that candidate; pick another card to switch.', target: null },
      { text: 'Read its settings line: the value of every knob the search chose.', target: null },
      { text: 'Compare net return, Sharpe and worst fall across the cards. More return with a much deeper fall is not a clear win.', target: null },
      { text: 'Check trades against the minimum. A card below its minimum rests on too few trades to judge.', target: null },
      { text: 'Glance at the small line: the same development return as the equity chart, at a glance.', target: null },
    ],
  },
  'equity-and-fall': {
    title: 'Equity and fall from peak',
    question: 'How did each candidate grow over the development period, and how deep were its falls?',
    steps: [
      { text: 'Read the labels at the right edge to see where each candidate finished.', target: { kind: 'point', group: 'equity', at: 'last' } },
      { text: 'Follow each line from left to right. A steady climb is more convincing than one big jump.', target: { kind: 'series', group: 'equity' } },
      {
        text: 'Drop to the lower part at the same date. A line below 0% there means that candidate is still recovering from a drop.',
        target: { kind: 'series', group: 'fall' },
      },
      { text: 'Find the lowest point of each line in the lower part. That is its deepest fall at a session close.', target: { kind: 'point', group: 'fall', at: 'lowest' } },
      { text: 'Note how long each line stays below 0%. Long stretches below a high are hard to sit through.', target: { kind: 'series', group: 'fall' } },
    ],
  },
  'side-by-side': {
    title: 'Candidates side by side',
    question: 'Which candidate leads on each measure?',
    steps: [
      { text: 'Find the selected candidate: its dots are the larger ones.', target: { kind: 'series', group: 'selected' } },
      { text: 'Read one row at a time. Each measure has its own scale, and further right is better on every row.', target: null },
      {
        text: 'Compare the other dots on the same row. A dot well to the right of the selected one marks a measure where that candidate leads.',
        target: { kind: 'series', group: 'other' },
      },
      { text: 'Weigh the rows together. A lead in return that comes with a deeper fall or fewer stress runs in profit is bought with risk.', target: null },
    ],
  },
  'neighbor-tornado': {
    title: 'Neighbor tornado',
    question: 'Does the result hold when one setting moves one step?',
    steps: [
      { text: 'Start at the dashed line at 0: the candidate’s own net return. Each bar shows how far a one-step move changes it.', target: null },
      { text: 'Read the lighter bars: each knob one step below the candidate’s value.', target: { kind: 'series', group: 'below' } },
      { text: 'Read the darker bars: each knob one step above.', target: { kind: 'series', group: 'above' } },
      { text: 'Look for a knob marked “loses money” and its red bar. A one-step change there turns the result into a loss.', target: null },
      { text: 'Hover a knob to see each step’s value and net return, and why a step has no bar.', target: null },
    ],
  },
  'cost-stress': {
    title: 'Cost stress ladder',
    question: 'Does the result survive harsher costs and fills?',
    steps: [
      { text: 'Start with the top bar: the result at the study’s own costs.', target: { kind: 'series', group: 'base' } },
      { text: 'Each bar below reruns the same settings with one harsher cost or fill.', target: { kind: 'series', group: 'stress' } },
      { text: 'Find the stressed bar with the lowest return. That stress is the one the result is most sensitive to.', target: { kind: 'point', group: 'stress', at: 'lowest' } },
      { text: 'A bar left of 0% marks a stress that turns the result into a loss.', target: null },
    ],
  },
  'concentration-curve': {
    title: 'Profit concentration curve',
    question: 'With the trades counted best first, how fast does the net profit pile up?',
    steps: [
      { text: 'A steep climb at the left means the best few trades carry much of the profit.', target: { kind: 'series', group: 'curve' } },
      { text: 'The marked dot shows the share of net profit the best 5% of trades make.', target: { kind: 'series', group: 'best' } },
      { text: 'The peak is where the winning trades end. Its height is all the winners added together.', target: null },
      { text: 'The fall from the peak back to 100% is all the losing trades added together.', target: { kind: 'series', group: 'curve' } },
    ],
  },
  'without-best': {
    title: 'Without its best',
    question: 'Does the result stay profitable without its best month or its best 5% of trades?',
    steps: [
      { text: 'Start from the top bar: the development net profit with every trade.', target: { kind: 'series', group: 'all' } },
      { text: 'Compare the middle bar with it. The gap is what the best month made.', target: { kind: 'series', group: 'month' } },
      { text: 'Compare the bottom bar with it. The gap is what the best 5% of trades made together.', target: { kind: 'series', group: 'trades' } },
      { text: 'A bar at $0 or less turns red. That is the concern the decision summary reports.', target: null },
    ],
  },
};
