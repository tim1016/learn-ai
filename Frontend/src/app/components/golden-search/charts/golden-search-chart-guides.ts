import type { HighlightTarget } from './golden-search-chart-spec';

/** Every chart with a guide section; the id is that section's anchor in the Golden Search guide. */
export type GoldenSearchChartId = 'equity-and-fall';

export interface WalkthroughStep {
  /** Word for word the guide's "How to read it" step; the guide contract spec holds the two together. */
  readonly text: string;
  readonly target: HighlightTarget;
}

export interface ChartGuide {
  /** The panel title, and the guide section's heading. */
  readonly title: string;
  /** The one-line question the panel answers. */
  readonly question: string;
  readonly steps: readonly WalkthroughStep[];
}

export const CHART_GUIDES: Readonly<Record<GoldenSearchChartId, ChartGuide>> = {
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
};
