import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { RunVerdict } from '../../../api/run-verdict.types';
import { ReadinessScoreCardComponent } from './readiness-score-card.component';

describe('ReadinessScoreCardComponent', () => {
  it('renders incomplete required coverage without a grade or deployment signal', async () => {
    const verdict: RunVerdict = {
      verdict_version: 2,
      status: 'incomplete',
      engine: 'lean',
      generated_at_ms: 1_700_000_000_000,
      composite: null,
      grade: null,
      signal: null,
      headline:
        'Production Readiness incomplete — 4/17 required metrics available. LEAN-native metrics remain separate evidence.',
      red_flags: [],
      dimensions: [
        {
          key: 'trade_edge',
          label: 'Trade Edge',
          weight: 20 / 85,
          score: null,
          summary: 'Is there a real per-trade edge after costs?',
          sub_scores: [
            {
              key: 'expectancy',
              label: 'Expectancy / trade',
              score: null,
              raw_value: null,
              display: '-',
              note: 'Not computed.',
            },
          ],
        },
      ],
      missing_metrics: ['Trade Edge: Expectancy / trade'],
      missing_required_metrics: [
        'Trade Edge: Expectancy / trade',
        'Statistical Confidence: Trade vs Portfolio Sharpe gap',
      ],
      available_required_metrics: 4,
      required_metrics: 17,
      normalized_weights: false,
      cleanliness: null,
    };

    await render(ReadinessScoreCardComponent, { inputs: { verdict } });

    expect(screen.getByText('Incomplete')).toBeTruthy();
    expect(screen.getByText('4/17')).toBeTruthy();
    expect(screen.queryByText(/Grade [A-F]/)).toBeNull();
    expect(screen.queryByText('Iterate')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /Show dimension breakdown/ }));
    expect(screen.getByText('Not graded — required evidence missing')).toBeTruthy();
    expect(screen.getByText(/Missing evidence is not scored/)).toBeTruthy();
  });
});
