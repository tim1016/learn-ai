import { provideZonelessChangeDetection } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { StrategyInfo } from '../lean-engine.component';
import { StrategyDetailTabComponent } from './strategy-detail-tab.component';

const STRATEGY: StrategyInfo = {
  name: 'spy_ema_crossover',
  display_name: 'SPY EMA Crossover',
  description: 'Fast/slow EMA cross on SPY.',
  params_schema: {},
  supported_resolutions: ['minute', 'daily'],
  gotchas: ['Warmup needs 200 bars'],
};

describe('StrategyDetailTabComponent', () => {
  it('renders the strategy contract and known constraints', async () => {
    await render(StrategyDetailTabComponent, {
      inputs: { strategy: STRATEGY },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByRole('heading', { name: 'SPY EMA Crossover' })).toBeTruthy();
    expect(screen.getByText('spy_ema_crossover')).toBeTruthy();
    expect(screen.getByText('minute, daily')).toBeTruthy();
    expect(screen.getByText('Warmup needs 200 bars')).toBeTruthy();
  });

  it('emits close and configure intents rather than mutating parent state', async () => {
    const { fixture } = await render(StrategyDetailTabComponent, {
      inputs: { strategy: STRATEGY },
      providers: [provideZonelessChangeDetection()],
    });
    let closed = 0;
    let configured = 0;
    fixture.componentInstance.closed.subscribe(() => (closed += 1));
    fixture.componentInstance.configure.subscribe(() => (configured += 1));

    fireEvent.click(screen.getByRole('button', { name: /Close tab/ }));
    expect(closed).toBe(1);

    fireEvent.click(screen.getByRole('button', { name: 'Configure this strategy' }));
    expect(configured).toBe(1);
  });

  it('shows an honest empty note when no constraints are registered', async () => {
    await render(StrategyDetailTabComponent, {
      inputs: { strategy: { ...STRATEGY, gotchas: [] } },
      providers: [provideZonelessChangeDetection()],
    });

    expect(
      screen.getByText('No strategy-specific constraints are registered.'),
    ).toBeTruthy();
  });
});
