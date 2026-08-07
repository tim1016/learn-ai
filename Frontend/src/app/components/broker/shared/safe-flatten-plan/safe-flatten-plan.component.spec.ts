import { render, screen } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { SqliteSafeFlattenPlan } from '../../../../api/alpaca.types';
import { SafeFlattenPlanComponent } from './safe-flatten-plan.component';

const PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17-exact',
  account_id: 'PA1',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'BOT',
  strategy_instance_id: 'spy-bot',
  reconciliation_id: 'reconciliation-17',
  prepared_at_ms: 1_700_000_010_000,
  expires_at_ms: 1_700_000_039_000,
  legs: [{
    strategy_instance_id: 'spy-bot',
    symbol: 'SPY',
    side: 'sell',
    quantity: 1.25,
    position_updated_at_ms: 1_700_000_008_000,
  }],
};

describe('SafeFlattenPlanComponent', () => {
  afterEach(() => vi.useRealTimers());

  it('renders the exact backend-authored plan without implying execution', async () => {
    await render(SafeFlattenPlanComponent, {
      inputs: { plan: { ...PLAN, expires_at_ms: Date.now() + 30_000 } },
    });

    expect(screen.getByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeTruthy();
    expect(screen.getByText('Spy')).toBeTruthy();
    expect(screen.getByText('Sell')).toBeTruthy();
    expect(screen.getByText('1.25')).toBeTruthy();
    expect(screen.getByText('spy-bot')).toBeTruthy();
    expect(screen.getByText('db-generation-4')).toBeTruthy();
    expect(screen.getByText('reconciliation-17')).toBeTruthy();
    expect(screen.getByText('plan-token-17-exact')).toBeTruthy();
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
    expect(screen.queryByText('Expired')).toBeNull();
  });

  it('marks the plan expired when its deadline passes', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(PLAN.prepared_at_ms);
    const { fixture } = await render(SafeFlattenPlanComponent, {
      inputs: { plan: { ...PLAN, expires_at_ms: PLAN.prepared_at_ms + 1_000 } },
    });

    vi.setSystemTime(PLAN.prepared_at_ms + 1_000);
    await vi.advanceTimersByTimeAsync(1_000);
    fixture.detectChanges();

    expect(screen.getByText('Expired')).toBeTruthy();
    expect(screen.getByText(/Prepare safe flatten again/)).toBeTruthy();
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
  });
});
