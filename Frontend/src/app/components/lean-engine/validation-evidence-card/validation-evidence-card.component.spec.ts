import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { ValidationEvidenceCardComponent } from './validation-evidence-card.component';

describe('ValidationEvidenceCardComponent', () => {
  it('renders the three validation gates and the current data-policy context', async () => {
    await render(ValidationEvidenceCardComponent, {
      inputs: { symbol: 'SPY', resolution: 'minute' },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText('Data policy matches')).toBeTruthy();
    expect(screen.getByText('Trades reconcile')).toBeTruthy();
    expect(screen.getByText('Backtest receipt')).toBeTruthy();
    expect(screen.getByText('SPY · minute · RTH · adjusted')).toBeTruthy();
  });

  it('does not assert a satisfied readiness state for any gate', async () => {
    const { container } = await render(ValidationEvidenceCardComponent, {
      inputs: { symbol: 'SPY', resolution: 'minute' },
      providers: [provideZonelessChangeDetection()],
    });

    // Honesty guard: the card explains the gates, it never renders an
    // unearned "ready ✓" — so no list item carries the ready marker class.
    expect(container.querySelectorAll('li.ready').length).toBe(0);
  });
});
