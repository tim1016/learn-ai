import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { ValidationEvidenceCardComponent } from './validation-evidence-card.component';

const emaPolicy = {
  source: 'polygon',
  symbol: 'SPY',
  adjusted: true,
  session: 'regular',
  input_bars: { timespan: 'minute', multiplier: 1 },
  strategy_bars: { timespan: 'minute', multiplier: 15 },
  timestamp_policy: 'bar_close_ms_utc',
  timezone: 'America/New_York',
  provider_kind: 'live',
  fixture_id: null,
  fixture_sha256: null,
} as const;

describe('ValidationEvidenceCardComponent', () => {
  it('renders the three validation gates and the current data-policy context', async () => {
    await render(ValidationEvidenceCardComponent, {
      inputs: { policy: emaPolicy },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText('Data policy matches')).toBeTruthy();
    expect(screen.getByText('Trades reconcile')).toBeTruthy();
    expect(screen.getByText('Backtest receipt')).toBeTruthy();
    expect(screen.getByText('Polygon input: 1-minute source bars → strategy: 15-minute decision bars · RTH · adjusted')).toBeTruthy();
    expect(screen.getByText('Signals are evaluated after consolidation, not on every source bar.')).toBeTruthy();
  });

  it('does not assert a satisfied readiness state for any gate', async () => {
    const { container } = await render(ValidationEvidenceCardComponent, {
      inputs: { policy: emaPolicy },
      providers: [provideZonelessChangeDetection()],
    });

    // Honesty guard: the card explains the gates, it never renders an
    // unearned "ready ✓" — so no list item carries the ready marker class.
    expect(container.querySelectorAll('li.ready').length).toBe(0);
  });
});
