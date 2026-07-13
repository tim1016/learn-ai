import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { ValidationStagePlaceholderComponent } from './validation-stage-placeholder.component';

describe('ValidationStagePlaceholderComponent', () => {
  it('reflects the current configuration and an honest empty message', async () => {
    await render(ValidationStagePlaceholderComponent, {
      inputs: {
        symbol: 'SPY',
        resolution: 'minute',
        fillMode: 'signal_bar_close',
        engine: 'both',
      },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText('SPY validation run')).toBeTruthy();
    expect(screen.getByText('signal close')).toBeTruthy();
    expect(screen.getByText('both')).toBeTruthy();
    expect(
      screen.getByText(/Run a validation to populate the equity curve/),
    ).toBeTruthy();
  });

  it('never fabricates trade markers or a preview chart', async () => {
    await render(ValidationStagePlaceholderComponent, {
      inputs: {
        symbol: 'SPY',
        resolution: 'minute',
        fillMode: 'next_bar_open',
        engine: 'python',
      },
      providers: [provideZonelessChangeDetection()],
    });

    // Honesty guard: no invented BUY/EXIT markers before a run exists.
    expect(screen.queryByText('BUY')).toBeNull();
    expect(screen.queryByText('EXIT')).toBeNull();
    expect(screen.getByText('next open')).toBeTruthy();
  });
});
